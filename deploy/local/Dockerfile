FROM python:3.12-slim

RUN apt-get update \
    && apt-get install --no-install-recommends -y ca-certificates docker.io git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/aep
COPY pyproject.toml ./
COPY src/ src/
COPY schemas/ schemas/
RUN python -m pip install --no-cache-dir .

ENV PYTHONUNBUFFERED=1 \
    PYTHONPATH=/opt/aep/src
CMD ["python", "-m", "aep.local_service", "serve"]
