"""Reconciliacion on-chain: la verdad esta en la cadena, no en nuestra BD.

Por que existe esto. La escritura de una operacion es atomica DENTRO de Postgres
(repositories.record_entry), pero la atomicidad no puede cruzar a un sistema
externo: si el proceso muere entre que el CLOB acepta una orden y Medusa la
persiste, existe una posicion REAL que Medusa no sabe que tiene. Una posicion asi
no se vende, no tiene stop-loss y no se cuenta en la exposicion: el Risk Manager
opera creyendo que hay menos dinero en riesgo del que hay.

Esto no se arregla con mas cuidado al escribir. Se arregla comparando contra la
cadena al arrancar y negandose a operar en Live si no cuadra.

En PAPER no aplica: no hay nada on-chain que reconciliar.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from medusa.config import get_settings


@dataclass
class ReconcileReport:
    """Diferencias entre lo que dice la cadena y lo que dice la BD."""

    # Existe on-chain pero no en la BD. EL PELIGROSO: dinero real en un mercado
    # que Medusa no vigila ni cerrara nunca.
    untracked: list[dict] = field(default_factory=list)
    # La BD la da por abierta pero on-chain no hay nada. La BD miente sobre la
    # exposicion (p.ej. una venta que se ejecuto y no se registro).
    phantom: list[dict] = field(default_factory=list)
    # Existe en ambos lados pero el numero de shares no coincide.
    mismatched: list[dict] = field(default_factory=list)
    # Cuadran.
    matched: list[dict] = field(default_factory=list)
    # Resueltas y cobrables on-chain: hay que redimirlas.
    redeemable: list[dict] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        return not (self.untracked or self.phantom or self.mismatched)

    def to_dict(self) -> dict:
        return {
            "clean": self.clean,
            "untracked": self.untracked,
            "phantom": self.phantom,
            "mismatched": self.mismatched,
            "redeemable": self.redeemable,
            "matched_count": len(self.matched),
            "summary": self.summary(),
        }

    def summary(self) -> str:
        if self.clean:
            return f"OK: {len(self.matched)} posiciones cuadran con la cadena"
        parts = []
        if self.untracked:
            parts.append(f"{len(self.untracked)} on-chain sin registrar")
        if self.phantom:
            parts.append(f"{len(self.phantom)} registradas que no existen on-chain")
        if self.mismatched:
            parts.append(f"{len(self.mismatched)} con tamaño distinto")
        return "DESCUADRE: " + ", ".join(parts)


def diff_positions(onchain: list[dict], db_positions: list[dict]) -> ReconcileReport:
    """Compara posiciones on-chain contra las de la BD. Funcion pura y testeable.

    `onchain`: payload de la Data API (asset, size, conditionId, title, ...).
    `db_positions`: filas de positions abiertas (token_id, size, ...).
    """
    s = get_settings()
    report = ReconcileReport()

    chain_by_token = {
        str(p.get("asset")): p
        for p in onchain
        if float(p.get("size") or 0) >= s.reconcile_min_shares
    }
    db_by_token = {str(p.get("token_id")): p for p in db_positions if p.get("token_id")}

    for token, chain_pos in chain_by_token.items():
        chain_size = float(chain_pos.get("size") or 0)
        if chain_pos.get("redeemable"):
            report.redeemable.append({
                "token_id": token,
                "market_id": chain_pos.get("conditionId", ""),
                "question": chain_pos.get("title", ""),
                "size": chain_size,
                "tracked": token in db_by_token,
            })

        db_pos = db_by_token.get(token)
        if db_pos is None:
            report.untracked.append({
                "token_id": token,
                "market_id": chain_pos.get("conditionId", ""),
                "question": chain_pos.get("title", ""),
                "outcome": chain_pos.get("outcome", ""),
                "size": chain_size,
                "avg_price": float(chain_pos.get("avgPrice") or 0),
                "value": float(chain_pos.get("currentValue") or 0),
            })
            continue

        db_size = float(db_pos.get("size") or 0)
        larger = max(abs(chain_size), abs(db_size), 1e-9)
        if abs(chain_size - db_size) / larger > s.reconcile_size_tolerance:
            report.mismatched.append({
                "token_id": token,
                "position_id": db_pos.get("id"),
                "question": db_pos.get("question", ""),
                "db_size": db_size,
                "chain_size": chain_size,
            })
        else:
            report.matched.append({"token_id": token, "position_id": db_pos.get("id")})

    for token, db_pos in db_by_token.items():
        if token not in chain_by_token:
            report.phantom.append({
                "token_id": token,
                "position_id": db_pos.get("id"),
                "question": db_pos.get("question", ""),
                "db_size": float(db_pos.get("size") or 0),
                "cost_basis": float(db_pos.get("cost_basis") or 0),
            })

    return report


class Reconciler:
    def __init__(self, client, log) -> None:
        self.client = client
        self.log = log
        self.s = get_settings()

    def _wallet(self) -> str:
        """Direccion que tiene los fondos.

        Con proxy (signature_type 1/2) las posiciones estan a nombre del funder,
        no de la clave que firma. Mirar la direccion equivocada devolveria una
        lista vacia y el reconciliador cantaria "todo limpio" mirando a la nada.
        """
        if self.s.clob_signature_type in (1, 2):
            return self.s.clob_funder or self.s.wallet_address
        return self.s.wallet_address

    async def run(self, db_positions: list[dict]) -> ReconcileReport:
        wallet = self._wallet()
        if not wallet:
            raise RuntimeError(
                "No hay WALLET_ADDRESS (ni CLOB_FUNDER) configurada: no se puede "
                "reconciliar contra la cadena"
            )
        onchain = await self.client.fetch_positions(
            wallet, min_shares=self.s.reconcile_min_shares
        )
        report = diff_positions(onchain, db_positions)
        self.log.info(
            "reconcile.done", onchain=len(onchain), db=len(db_positions),
            clean=report.clean, untracked=len(report.untracked),
            phantom=len(report.phantom), mismatched=len(report.mismatched),
        )
        return report
