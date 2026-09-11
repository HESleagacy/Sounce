FROM python:3.12-slim-bookworm

RUN apt-get update && apt-get install -y --no-install-recommends \
    dumb-init \
    ca-certificates \
    libmagic1 \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Hash-pinned lock, generated from pyproject.toml with:
#   pip-compile --generate-hashes --strip-extras --output-file=requirements.lock pyproject.toml
# --require-hashes makes the build fail rather than silently install something else.
COPY requirements.lock ./
RUN pip install --no-cache-dir --require-hashes -r requirements.lock

COPY . .

RUN mkdir -p /app/data \
    && useradd --create-home botuser \
    && chown -R botuser:botuser /app
USER botuser

# Health-check port for container orchestrators (Railway / Fly.io set PORT).
ENV PORT=8080
EXPOSE 8080

ENTRYPOINT ["dumb-init", "--"]
CMD ["python", "-m", "app.main"]
