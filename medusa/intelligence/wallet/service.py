"""Servicio de Wallet Intelligence: descubrir -> ingerir -> perfilar -> persistir.

Unica pieza del subsistema que habla con la red, con la BD y con el reloj. Todo
lo que decide algo (metricas, score, reputacion, clusters, similitud) vive en
modulos puros y se testea sin infraestructura.

CONTRATO (identico al del Intelligence Layer V3 y al del MIG):

  - No importa `medusa.execution`, `medusa.trading`, `medusa.risk` ni
    `medusa.strategies`. No tiene con que operar aunque quisiera.
  - No escribe una sola fila fuera de las tablas `wi_*`.
  - Loop propio, con timeout. El ciclo de trading jamas lo espera.
  - Apagado por defecto (`WALLET_INTEL_ENABLED=false`).

ESTO NO ES COPY TRADING. El resultado de todo el pipeline son numeros por
wallet y, a traves del modulo del Intelligence Layer, features por MERCADO.
Ninguna funcion de este fichero devuelve un lado, un tamaño ni un precio.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import time

from medusa.config import get_settings
from medusa.data import repositories as repo
from medusa.intelligence.wallet import ingest
from medusa.intelligence.wallet import repository as wi_repo
from medusa.intelligence.wallet.feed import WalletFeed
from medusa.intelligence.wallet.types import PopulationStats, WalletDNA, WalletPosition
from medusa.intelligence.wallet.wallet_clusters import cluster_wallets
from medusa.intelligence.wallet.wallet_dna import (
    build_population,
    population_stats,
)
from medusa.intelligence.wallet.wallet_reputation import reputation_population
from medusa.intelligence.wallet.wallet_scoring import feature_importance
from medusa.intelligence.wallet.wallet_similarity import similarity_edges
from medusa.logging_setup import err


class WalletIntelligenceService:
    def __init__(self, log, publish_log=None) -> None:
        self.log = log
        self.s = get_settings()
        self._publish = publish_log
        self._feed: WalletFeed | None = None
        self.last_run: dict | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.s.wallet_intel_enabled)

    def info(self) -> dict:
        from medusa.intelligence.wallet.types import DNA_DEFINITIONS, DNA_FEATURES

        return {
            "enabled": self.enabled,
            "interval_seconds": self.s.wallet_intel_interval,
            "max_wallets": self.s.wallet_max_wallets,
            "max_markets": self.s.wallet_max_markets,
            "min_samples": self.s.wallet_min_samples,
            "recent_days": self.s.wallet_recent_days,
            "half_life_days": self.s.wallet_half_life_days,
            "clusters_k": self.s.wallet_clusters_k,
            "min_similarity": self.s.wallet_min_similarity,
            "retention_days": self.s.wallet_retention_days,
            "dna_features": list(DNA_FEATURES),
            "dna_definitions": DNA_DEFINITIONS,
            "is_copy_trading": False,
            "can_place_orders": False,
            "last_run": self.last_run,
        }

    def _get_feed(self) -> WalletFeed:
        if self._feed is None:
            self._feed = WalletFeed(self.log)
        return self._feed

    async def close(self) -> None:
        if self._feed is not None:
            await self._feed.close()
            self._feed = None

    # ------------------------------------------------------------ ingesta --
    async def discover_wallets(self) -> list[str]:
        """Descubre wallets a partir de los mercados que Medusa ya vigila.

        No hay lista de wallets en ningun sitio: se deduce de los holders de los
        mercados mejor puntuados. Eso ancla la poblacion a lo que el bot mira de
        verdad, en vez de a un universo arbitrario.
        """
        markets = await repo.list_ranked_markets(limit=self.s.wallet_max_markets)
        if not markets:
            markets = await repo.list_markets(limit=self.s.wallet_max_markets)
        feed = self._get_feed()
        found: set[str] = set()
        for m in markets:
            if len(found) >= self.s.wallet_max_wallets:
                break
            holders = await feed.fetch_holders(
                str(m.get("id") or ""), limit=self.s.wallet_holders_per_market
            )
            found.update(ingest.extract_wallets(holders, self.s.wallet_min_holder_size))
        return sorted(found)[: self.s.wallet_max_wallets]

    async def load_positions(self, wallets: list[str]) -> dict[str, list[WalletPosition]]:
        """Posiciones normalizadas de cada wallet.

        Una wallet que falle se queda FUERA en vez de entrar con datos a medias:
        un perfil construido sobre media historia es peor que ningun perfil,
        porque nadie sabria que le falta la mitad.
        """
        feed = self._get_feed()
        out: dict[str, list[WalletPosition]] = {}
        for wallet in wallets:
            try:
                positions = await feed.fetch_positions(wallet, limit=self.s.wallet_max_positions)
                if not positions:
                    continue
                activity = await feed.fetch_activity(wallet, limit=self.s.wallet_max_activity)
                cids = [str(p.get("conditionId") or p.get("market") or "") for p in positions]
                meta = await feed.fetch_market_meta([c for c in cids if c])
                rows = ingest.normalize_positions(
                    wallet, positions, activity=activity, market_meta=meta
                )
                if rows:
                    out[wallet] = rows
            except Exception as exc:  # noqa: BLE001 - una wallet mala no tumba la pasada
                self.log.warning("wallet.load_failed", wallet=wallet, error=err(exc))
        return out

    # ----------------------------------------------------------- perfilado --
    def profile(
        self, positions_by_wallet: dict[str, list[WalletPosition]], *,
        now: dt.datetime | None = None,
    ) -> dict:
        """ADN + score + reputacion + clusters + similitud. PURO (sin BD, sin red).

        Se expone aparte de `run()` para poder verificar el pipeline entero con
        datos sinteticos, y para que la API pueda ofrecer una vista previa sin
        escribir nada.
        """
        now = now or dt.datetime.now(dt.timezone.utc)
        dnas: list[WalletDNA] = build_population(
            positions_by_wallet, now=now,
            recent_days=self.s.wallet_recent_days,
            half_life_days=self.s.wallet_half_life_days,
            bucket_days=self.s.wallet_bucket_days,
        )
        pop: PopulationStats = population_stats(dnas)
        reputations = reputation_population(dnas, pop, min_samples=self.s.wallet_min_samples)
        clusters = cluster_wallets(
            dnas, pop, k=self.s.wallet_clusters_k, min_wallets=self.s.wallet_min_cluster_wallets
        )
        pairs = similarity_edges(
            dnas, pop, min_similarity=self.s.wallet_min_similarity,
            top_k=self.s.wallet_similarity_top_k,
        )
        importance = feature_importance(dnas, pop)
        by_wallet = {d.wallet: d for d in dnas}
        assignments = clusters.get("assignments", {})

        profiles = []
        for rep in reputations:
            dna = by_wallet[rep["wallet"]]
            profiles.append({
                "wallet": dna.wallet,
                "dna": {k: round(float(v), 6) for k, v in dna.metrics.items()},
                "score": rep["score"],
                "reputation": rep["reputation"],
                "cluster": int(assignments.get(dna.wallet, -1)),
                "n_positions": dna.n_positions,
                "n_closed": dna.n_closed,
                "n_markets": dna.n_markets,
                "n_categories": dna.n_categories,
                "categories": dna.categories,
                "first_trade": dna.first_trade,
                "last_trade": dna.last_trade,
                "factors": {k: rep[k] for k in ("sample_factor", "freshness", "stability")},
                "contributions": rep["contributions"],
            })
        return {
            "profiles": profiles,
            "population": pop.to_dict(),
            "clusters": clusters,
            "similarity": pairs,
            "feature_importance": importance,
        }

    # -------------------------------------------------------------- pasada --
    async def run(self, persist: bool = True) -> dict:
        """Pasada completa. No captura excepciones: quien llama decide."""
        started = time.time()
        wallets = await self.discover_wallets()
        positions_by_wallet = await self.load_positions(wallets)
        result = self.profile(positions_by_wallet)
        elapsed = time.time() - started
        n_positions = sum(len(v) for v in positions_by_wallet.values())

        written = {"profiles": 0, "history": 0, "similarity": 0, "clusters": 0}
        if persist and result["profiles"]:
            written["profiles"] = await wi_repo.upsert_profiles(result["profiles"])
            written["history"] = await wi_repo.save_dna_history(result["profiles"])
            written["similarity"] = await wi_repo.upsert_similarity(result["similarity"])
            written["clusters"] = await wi_repo.save_clusters(
                result["clusters"].get("clusters", [])
            )
            await wi_repo.save_run(
                wallets=len(result["profiles"]), positions=n_positions,
                clusters=result["clusters"].get("k", 0),
                similarity_pairs=len(result["similarity"]),
                build_seconds=elapsed, population=result["population"],
                feature_importance=result["feature_importance"],
            )

        summary = {
            "ok": True,
            "persisted": persist,
            "build_seconds": round(elapsed, 3),
            "wallets_discovered": len(wallets),
            "wallets_profiled": len(result["profiles"]),
            "positions": n_positions,
            "clusters": result["clusters"].get("k", 0),
            "similarity_pairs": len(result["similarity"]),
            "written": written,
            "top_features": result["feature_importance"][:5],
        }
        self.last_run = {k: summary[k] for k in
                         ("build_seconds", "wallets_profiled", "positions", "clusters")}
        self.log.info("wallet.run_done", **self.last_run)
        return summary

    async def run_guarded(self) -> dict | None:
        """`run()` con timeout y sin propagar: la variante del loop del engine."""
        try:
            return await asyncio.wait_for(self.run(), timeout=self.s.wallet_intel_timeout)
        except asyncio.TimeoutError:
            self.log.warning("wallet.run_timeout", timeout=self.s.wallet_intel_timeout)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - jamas tumba el engine
            self.log.error("wallet.run_failed", error=err(exc), exc_info=True)
        return None

    async def prune(self) -> dict:
        return await wi_repo.prune(self.s.wallet_retention_days)
