FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    DATA_DIR=/data \
    CONFIG_PATH=/app/config.yaml \
    PROFILE_PATH=/app/profile/profile.yaml \
    PORT=8080

WORKDIR /app

# Dependencies and Chromium first so code edits don't redo the slow layer.
COPY pyproject.toml ./
RUN mkdir autoapply && touch autoapply/__init__.py \
    && pip install . \
    && playwright install --with-deps chromium \
    && pip uninstall -y autoapply

COPY autoapply ./autoapply
RUN pip install --no-deps .

COPY config.yaml boards.yaml ./
COPY profile ./profile

EXPOSE 8080
CMD ["autoapply", "serve"]
