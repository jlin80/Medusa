"""Market Scanner global: recorre TODO el universo activo de Polymarket,
clasifica cada mercado por categoria, lo pre-puntua y enriquece el top del
ranking con datos profundos (order books, historico).

Dos niveles, a proposito:

  scan_universe()  -> barato. Pagina la Gamma API (sin tocar el CLOB), clasifica
                      (MarketClassifier), puntua (OpportunityPreScorer) y
                      persiste el ranking. Se ejecuta cada global_scan_interval.
  deep_enrich()    -> caro. Pide order books (y historico si alguna estrategia
                      lo necesita) SOLO para el top-K del ranking. Se ejecuta
                      cada ciclo de trading.

Esta division es lo que permite mirar cientos de mercados con 2 vCPU y sin
castigar las APIs publicas: el 95% del universo se descarta con datos que ya
venian en la respuesta de Gamma.
"""

from __future__ import annotations

import time

from medusa.config import get_settings
from medusa.core.models import Market, MarketContext
from medusa.data import repositories as repo
from medusa.data.polymarket.client import PolymarketClient
from medusa.intelligence import MarketClassifier, OpportunityPreScorer
from medusa.intelligence.prescorer import ScoredMarket
from medusa.logging_setup import err

# El cache de historicos no debe crecer sin techo si el ranking rota mucho.
_HISTORY_CACHE_MAX = 200


class MarketScanner:
    def __init__(self, client: PolymarketClient, log) -> None:
        self.client = client
        self.log = log
        self.s = get_settings()
        self.classifier = MarketClassifier(log)
        self.prescorer = OpportunityPreScorer(log)
        # token_id -> (fetched_at, history). El historico de un mercado no
        # cambia de forma en 60s; pedirlo cada ciclo seria puro desperdicio.
        self._history_cache: dict[str, tuple[float, list[tuple[float, float]]]] = {}

    # ---------------------------------------------------------- nivel barato --
    async def scan_universe(self) -> list[ScoredMarket]:
        """Universo global -> clasificar -> puntuar -> ranking persistido."""
        markets = await self.client.fetch_all_markets(self.s.scan_universe_size)
        for m in markets:
            m.medusa_category = self.classifier.classify(m)

        ranked = self.prescorer.rank(markets)
        for sm in ranked:
            sm.market.opportunity_score = sm.score

        # Persistir solo lo escaneable: el resto del universo no aporta nada al
        # dashboard y quintuplicaria las escrituras en la CPU debil del CT202.
        await repo.upsert_markets([sm.market for sm in ranked])

        by_cat: dict[str, int] = {}
        for sm in ranked:
            by_cat[sm.market.medusa_category] = by_cat.get(sm.market.medusa_category, 0) + 1
        self.log.info(
            "scan.universe", fetched=len(markets), scannable=len(ranked),
            categories=by_cat,
        )
        return ranked

    # ------------------------------------------------------------ nivel caro --
    async def deep_enrich(
        self,
        top: list[ScoredMarket],
        need_book_no: bool = False,
        need_history: bool = False,
    ) -> list[tuple[Market, MarketContext]]:
        """Order books (y opcionalmente historico) para el top del ranking.

        Refresca precio/spread del mercado con el libro REAL del CLOB (los
        datos de Gamma pueden venir rezagados) y construye el MarketContext que
        consumen las estrategias.
        """
        enriched: list[tuple[Market, MarketContext]] = []
        for sm in top:
            m = sm.market
            ctx = MarketContext(prescore=sm.score, components=sm.components)
            try:
                book = await self.client.fetch_order_book(m.yes_token_id)
                ctx.book_yes = book
                m.best_bid = book.best_bid
                m.best_ask = book.best_ask
                m.spread = book.spread
                if book.mid > 0:
                    m.yes_price = round(book.mid, 4)
                    m.no_price = round(1 - m.yes_price, 4)
            except Exception as exc:  # noqa: BLE001 - un libro caido no rompe el ciclo
                self.log.warning("scan.book_fail", market=m.id, error=err(exc))
                continue   # sin libro YES no hay analisis profundo que valga

            if need_book_no and m.no_token_id:
                try:
                    ctx.book_no = await self.client.fetch_order_book(m.no_token_id)
                except Exception as exc:  # noqa: BLE001
                    self.log.warning("scan.book_no_fail", market=m.id, error=err(exc))

            if need_history:
                ctx.history = await self._history(m.yes_token_id)

            await repo.upsert_market(m)
            enriched.append((m, ctx))
        return enriched

    async def _history(self, token_id: str) -> list[tuple[float, float]]:
        now = time.time()
        cached = self._history_cache.get(token_id)
        if cached and now - cached[0] < self.s.history_cache_ttl:
            return cached[1]
        try:
            history = await self.client.fetch_price_history(token_id)
        except Exception as exc:  # noqa: BLE001
            self.log.warning("scan.history_fail", token=token_id, error=err(exc))
            return cached[1] if cached else []
        if len(self._history_cache) >= _HISTORY_CACHE_MAX:
            oldest = min(self._history_cache, key=lambda k: self._history_cache[k][0])
            del self._history_cache[oldest]
        self._history_cache[token_id] = (now, history)
        return history
