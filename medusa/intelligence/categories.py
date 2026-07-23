"""Taxonomia de categorias de mercados de Polymarket.

TODO el conocimiento de categorias vive aqui como DATOS (keywords y mapeos),
no como logica: para añadir una categoria nueva basta con añadir una entrada a
CATEGORY_KEYWORDS (y opcionalmente a GAMMA_CATEGORY_MAP y CATEGORY_PRIORITY).
Ni el clasificador ni el resto del sistema cambian.

Las keywords se comparan con limite de palabra (\\b) sobre question+slug en
minusculas: "eth" no debe disparar dentro de "something".
"""

from __future__ import annotations

# Categorias canonicas. "other" es el cajon de sastre y no necesita keywords.
CRYPTO = "crypto"
SPORTS = "sports"
POLITICS = "politics"
ECONOMY = "economy"
TECH = "tech"
AI = "ai"
CLIMATE = "climate"
GEOPOLITICS = "geopolitics"
ENTERTAINMENT = "entertainment"
OTHER = "other"

# Orden de desempate cuando varias categorias matchean: gana la que aparezca
# antes. Las mas especificas van primero (un mercado de "AI regulation bill"
# es de IA aunque mencione politica; una guerra es geopolitica aunque
# mencione a presidentes).
CATEGORY_PRIORITY: tuple[str, ...] = (
    AI, CRYPTO, SPORTS, GEOPOLITICS, CLIMATE, ECONOMY,
    ENTERTAINMENT, TECH, POLITICS, OTHER,
)

CATEGORY_KEYWORDS: dict[str, tuple[str, ...]] = {
    AI: (
        "ai", "artificial intelligence", "openai", "chatgpt", "gpt", "anthropic",
        "claude", "gemini", "deepmind", "llm", "agi", "superintelligence",
        "deepseek", "xai", "grok", "midjourney", "machine learning",
    ),
    CRYPTO: (
        "bitcoin", "btc", "ethereum", "eth", "solana", "sol", "crypto",
        "cryptocurrency", "dogecoin", "doge", "xrp", "ripple", "cardano", "ada",
        "binance", "coinbase", "stablecoin", "usdc", "usdt", "tether",
        "memecoin", "altcoin", "blockchain", "defi", "nft", "satoshi",
        "microstrategy", "etf approval", "halving", "airdrop", "polymarket",
    ),
    SPORTS: (
        "nba", "nfl", "mlb", "nhl", "ufc", "mma", "premier league", "la liga",
        "champions league", "world cup", "super bowl", "superbowl", "olympics",
        "grand slam", "wimbledon", "us open", "roland garros", "f1",
        "formula 1", "grand prix", "boxing", "heavyweight", "playoffs",
        "finals mvp", "touchdown", "home run", "golf", "pga", "masters",
        "tennis", "soccer", "football", "basketball", "baseball", "hockey",
        "cricket", "rugby", "messi", "ronaldo", "serie a", "bundesliga",
        "copa america", "euro 2024", "euro 2028", "march madness", "ncaa",
        "wnba", "esports", "league of legends", "dota", "cs2", "csgo",
        # El formato "X vs Y" es la firma de los mercados de partido (tenis,
        # etc.): sin esto, medio circuito ATP/WTA caia en "other" (medido en el
        # dry-run del 2026-07-16). Si "vs" aparece en una pregunta politica
        # ("Trump vs Newsom poll"), las keywords politicas la ganan por numero
        # de matches: el desempate por conteo lo absorbe.
        "vs", "vs.",
    ),
    GEOPOLITICS: (
        "war", "ceasefire", "invasion", "invade", "missile", "nuclear",
        "ukraine", "russia", "putin", "zelensky", "israel", "gaza", "hamas",
        "hezbollah", "iran", "north korea", "taiwan", "nato", "sanctions",
        "military strike", "airstrike", "troops", "territory", "annex",
        "peace deal", "peace agreement", "hostage", "houthi", "syria",
        "coup", "regime",
    ),
    CLIMATE: (
        "climate", "temperature", "hurricane", "storm", "tornado", "wildfire",
        "earthquake", "flood", "drought", "heat wave", "heatwave", "snowfall",
        "rainfall", "el nino", "la nina", "carbon", "emissions", "cop29",
        "cop30", "warmest", "hottest year", "sea level", "degrees",
        "weather",
    ),
    ECONOMY: (
        "fed", "federal reserve", "interest rate", "rate cut", "rate hike",
        "inflation", "cpi", "gdp", "recession", "unemployment", "jobs report",
        "nonfarm", "payrolls", "treasury", "bond", "yield", "s&p 500", "s&p",
        "nasdaq", "dow jones", "stock market", "market cap", "ipo",
        "earnings", "bankruptcy", "default", "debt ceiling", "tariff",
        "tariffs", "trade deal", "oil price", "opec", "gold price", "ecb",
        "boj", "yen", "euro", "dollar index", "fomc", "powell",
    ),
    ENTERTAINMENT: (
        "oscar", "oscars", "academy award", "grammy", "grammys", "emmy",
        "golden globe", "box office", "movie", "film", "album", "billboard",
        "spotify", "netflix", "hbo", "disney", "taylor swift", "beyonce",
        "drake", "kanye", "mrbeast", "youtube", "tiktok", "twitch",
        "eurovision", "bachelor", "survivor", "big brother", "celebrity",
        "kardashian", "royal family", "time person of the year",
        "album of the year", "song of the year", "rotten tomatoes",
    ),
    TECH: (
        "apple", "iphone", "google", "microsoft", "amazon", "meta", "facebook",
        "instagram", "twitter", "tesla", "spacex", "starship", "nvidia",
        "intel", "amd", "semiconductor", "chip", "smartphone", "app store",
        "cybertruck", "self-driving", "autonomous", "robotaxi", "vr",
        "quantum computing", "satellite", "rocket launch", "nasa",
        "moon landing", "mars", "starlink", "data breach", "hack",
    ),
    POLITICS: (
        "election", "president", "presidential", "senate", "congress", "house",
        "governor", "mayor", "primary", "primaries", "nominee", "nomination",
        "trump", "biden", "harris", "vance", "desantis", "newsom", "aoc",
        "supreme court", "scotus", "impeach", "impeachment", "cabinet",
        "executive order", "veto", "pardon", "poll", "approval rating",
        "electoral", "ballot", "referendum", "parliament", "prime minister",
        "chancellor", "legislation", "bill pass", "shutdown", "filibuster",
        "midterm", "swing state", "democrat", "republican", "gop", "labour",
        "tory", "confirmation",
    ),
}

# Mapeo de la etiqueta `category` que devuelve la Gamma API (cuando viene) a
# nuestra taxonomia. Gamma es inconsistente (a veces vacia, a veces generica),
# por eso es solo una PISTA que se combina con las keywords, no un veredicto.
GAMMA_CATEGORY_MAP: dict[str, str] = {
    "crypto": CRYPTO,
    "cryptocurrency": CRYPTO,
    "sports": SPORTS,
    "sport": SPORTS,
    "esports": SPORTS,
    "politics": POLITICS,
    "us-current-affairs": POLITICS,
    "us current affairs": POLITICS,
    "elections": POLITICS,
    "economy": ECONOMY,
    "economics": ECONOMY,
    "business": ECONOMY,
    "finance": ECONOMY,
    "markets": ECONOMY,
    "tech": TECH,
    "technology": TECH,
    "science": TECH,
    "ai": AI,
    "artificial intelligence": AI,
    "climate": CLIMATE,
    "weather": CLIMATE,
    "geopolitics": GEOPOLITICS,
    "world": GEOPOLITICS,
    "war": GEOPOLITICS,
    "middle east": GEOPOLITICS,
    "entertainment": ENTERTAINMENT,
    "pop-culture": ENTERTAINMENT,
    "pop culture": ENTERTAINMENT,
    "culture": ENTERTAINMENT,
    "music": ENTERTAINMENT,
    "movies": ENTERTAINMENT,
}


def all_categories() -> tuple[str, ...]:
    """Todas las categorias conocidas, en orden de prioridad."""
    return CATEGORY_PRIORITY
