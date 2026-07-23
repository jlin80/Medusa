# ---------------------------------------------------------------------------
# Dashboard: Nginx sirviendo estaticos + proxy inverso a la API.
#
# DECISION: el diseño original preveia una SPA en React+Vite compilada fuera de
# CT202. Se descarto: el dashboard son ~10 vistas de solo lectura mas un toggle,
# y en vanilla JS cabe en un fichero de ~30 KB sin build, sin node_modules y sin
# paso de compilacion que mantener. Menos partes moviles para el mismo resultado.
# ---------------------------------------------------------------------------
FROM nginx:alpine

COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY html/ /usr/share/nginx/html/

EXPOSE 80
