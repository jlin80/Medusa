# Medusa

Bot profesional de trading automatizado para **Polymarket**.
Se despliega 100% con Docker en el homelab (Proxmox **CT202**), que actúa solo
como host Docker.

> Estado: **F0 — Infraestructura base**. Sin lógica de trading todavía.
> Arranca siempre en **PAPER MODE**. LIVE requiere superar la validación de
> 7–14 días de paper continuo y activarse desde el dashboard con confirmación.

## Arranque rápido

```bash
cp .env.example .env          # edita credenciales y DISCORD_WEBHOOK_URL
docker compose build
docker compose up -d
docker compose ps             # todos los servicios deben quedar "healthy"
```

Dashboard: `http://<IP-CT202>:8080` (solo LAN/VPN — no hay login por diseño).

## Servicios (contenedores)

| Servicio  | Rol                                             | Puerto |
|-----------|-------------------------------------------------|--------|
| postgres  | Estado durable (mercados, órdenes, trades…)     | interno |
| redis     | Bus de eventos + cache + flags (modo/kill)      | interno |
| engine    | Cerebro: scanner→predicción→riesgo→ejecución    | —      |
| api       | FastAPI: REST + WebSocket + toggle              | interno |
| dashboard | Nginx + SPA (estado, PnL, logs, toggle)         | 8080   |

## Estructura

```
medusa/           paquete Python del backend (engine + api + módulos)
  core/           enums, estado (modo/kill-switch), eventos
  infra/          conexión Postgres y Redis
  data/           conectores de datos (Polymarket)   [esqueleto]
  scanner/        Market Scanner                      [esqueleto]
  prediction/     Prediction Engine                   [esqueleto]
  risk/           Risk Manager                        [esqueleto]
  trading/        Trading Engine                      [esqueleto]
  execution/      ExecutionAdapter: paper / live      [interfaz]
  notifications/  Discord                             [esqueleto]
  api/            FastAPI app
docker/           Dockerfile.core + entrypoint
dashboard/        Nginx + estáticos
scripts/          healthchecks, backup, deploy
db/               migraciones (Alembic)
tests/            pruebas
```

## Modos

- **PAPER** (por defecto): datos reales de Polymarket, ejecución simulada con
  fees, spread, slippage, liquidez y fills parciales. Sin dinero real.
- **LIVE**: misma lógica validada en paper; wallet conectada; activación manual
  desde el dashboard con confirmación y solo tras el gate de validación.

## Documentación viva

El progreso se registra siempre (append, nunca sobrescribir) en
`medusa.txt` dentro de este directorio.
