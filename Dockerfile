# Imagen de la propia API.
#
# Multi-stage: el builder instala las dependencias con uv en un .venv y el
# runtime solo copia ese .venv + el código. Ambas etapas usan la MISMA imagen
# base para que los symlinks del venv (python) sigan siendo válidos.
#
# Detalle de caché: se copian pyproject.toml + uv.lock ANTES que el código,
# así la capa de dependencias (la lenta) solo se reconstruye si cambian las
# dependencias, no en cada cambio de código.

FROM python:3.12-slim AS builder

# uv se copia desde su imagen oficial (binario estático, sin pip install)
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app
COPY pyproject.toml uv.lock ./
# --no-dev: sin pytest/ruff/moto en la imagen final. El proyecto no se
# empaqueta (no hay build-system): uv instala solo las dependencias.
RUN uv sync --frozen --no-dev


FROM python:3.12-slim

# Usuario no privilegiado. Si se monta el socket del daemon para usar el
# adaptador local desde dentro del contenedor, hay que darle acceso al grupo
# propietario del socket en el anfitrión (`--group-add` al ejecutar).
RUN useradd --create-home --uid 10001 apiuser

WORKDIR /app
COPY --from=builder /app/.venv ./.venv
COPY app ./app
COPY ui ./ui

ENV PATH="/app/.venv/bin:$PATH"
USER apiuser
EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
