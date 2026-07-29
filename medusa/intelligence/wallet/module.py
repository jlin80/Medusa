"""Puente al INTELLIGENCE LAYER: de reputaciones de wallet a FEATURES de mercado.

Aqui es donde Wallet Intelligence cumple su unico proposito declarado: producir
features. El modulo hereda de `IntelligenceModule`, asi que por construccion lo
maximo que puede devolver es `list[Feature]` -- no recibe el adapter de
ejecucion, ni el repositorio de posiciones, ni el engine. Es la misma frontera
que ya separa "prometemos que no opera" de "no puede operar".

Features que produce por mercado (todas float, como manda el contrato):

    wallet_reputation_mean   media de la reputacion de los holders conocidos
    wallet_reputation_max    la mejor reputacion presente en ese mercado
    wallet_reputation_weighted  media ponderada por el tamaño de cada holder
    wallet_known_holders     cuantos holders tienen perfil (el TAMAÑO DE MUESTRA;
                             sin el, una media sobre 1 wallet pareceria igual de
                             solida que una sobre 40)
    wallet_coverage          fraccion de holders del mercado con perfil conocido

POR QUE ESTO NO ES COPY TRADING: la feature dice "en este mercado hay wallets con
buen historial", no "compra lo que compraron". No mira el LADO de nadie
(YES/NO), no mira su precio de entrada y no propone tamaño. Una estrategia puede
consumirla como una variable mas, junto a la microestructura, y sigue siendo
ella quien decide -- con el Risk Manager despues, como siempre.

Deliberadamente NO se emite el lado agregado de las wallets buenas. Esa feature
si seria una señal de copia disfrazada de numero, y ademas seria mala: el
proyecto ya midio que el precio de Polymarket esta bien calibrado y que seguir
al consenso solo paga spread.
"""

from __future__ import annotations

from medusa.core.models import Market
from medusa.intelligence.wallet import ingest
from medusa.intelligence.wallet import repository as wi_repo
from medusa.intelligence.wallet.feed import WalletFeed
from medusa.intelligence.wallet.stats import mean, safe_div
from medusa.intelligence_layer.base import Feature, IntelligenceModule
from medusa.logging_setup import err


class WalletIntelligence(IntelligenceModule):
    name = "wallet"
    # La reputacion de una wallet se mueve en dias, no en minutos: recalcular
    # esto al ritmo de la microestructura seria tirar llamadas a la Data API.
    interval = 3600.0
    timeout = 120.0
    needs_network = True
    description = (
        "Reputacion agregada de los holders de cada mercado. Produce features, "
        "nunca ordenes. No es copy trading: no mira el lado de nadie."
    )

    def __init__(self, log) -> None:
        super().__init__(log)
        self._feed: WalletFeed | None = None

    def _get_feed(self) -> WalletFeed:
        if self._feed is None:
            self._feed = WalletFeed(self.log)
        return self._feed

    async def close(self) -> None:
        if self._feed is not None:
            await self._feed.close()
            self._feed = None

    async def compute(self, markets: list[Market], ctx: dict) -> list[Feature]:
        if not markets:
            return []
        feed = self._get_feed()
        out: list[Feature] = []

        for market in markets[: int(self.s.wallet_feature_max_markets)]:
            try:
                holders = await feed.fetch_holders(
                    market.id, limit=int(self.s.wallet_holders_per_market)
                )
            except Exception as exc:  # noqa: BLE001 - un mercado malo no tumba el modulo
                self.log.warning("wallet.module_holders_failed",
                                 market=market.id, error=err(exc))
                continue
            if not holders:
                continue

            addresses = ingest.extract_wallets(holders)
            if not addresses:
                continue

            # Tamaño por wallet, para la media ponderada. Si no viene, se pondera
            # a 1: sin dato, todos los holders pesan igual (nunca 0, que
            # borraria al holder de la media sin decirlo).
            sizes = {}
            for row in holders:
                addr = ingest._s(row, "proxyWallet", "wallet", "user", "address", "owner").lower()
                if addr:
                    sizes[addr] = max(0.0, ingest._f(row, "amount", "size", "shares", default=1.0))

            reputations: list[float] = []
            weights: list[float] = []
            for addr in addresses:
                profile = await wi_repo.get_profile(addr)
                if profile is None:
                    continue
                reputations.append(float(profile.get("reputation") or 0.0))
                weights.append(sizes.get(addr, 1.0))

            known = len(reputations)
            if known == 0:
                # Sin ningun holder perfilado NO se emite feature. Un 0.0 aqui
                # se leeria como "este mercado esta lleno de wallets malas",
                # que es una afirmacion distinta de "no sabemos nada de ellas".
                continue

            total_w = sum(weights)
            weighted = (safe_div(sum(r * w for r, w in zip(reputations, weights)), total_w)
                        if total_w > 0 else mean(reputations))
            base = {"known_holders": known, "total_holders": len(addresses)}
            out.append(self._feature(market.id, "wallet_reputation_mean",
                                     mean(reputations), **base))
            out.append(self._feature(market.id, "wallet_reputation_max",
                                     max(reputations), **base))
            out.append(self._feature(market.id, "wallet_reputation_weighted",
                                     weighted, **base))
            out.append(self._feature(market.id, "wallet_known_holders", float(known), **base))
            out.append(self._feature(market.id, "wallet_coverage",
                                     safe_div(known, len(addresses)), **base))
        return out
