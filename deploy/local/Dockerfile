FROM python:3.12-slim

WORKDIR /opt/aep
COPY pyproject.toml ./
COPY src/ src/
COPY schemas/ schemas/
RUN python -m pip install --no-cache-dir .

ENV PYTHONUNBUFFERED=1
CMD ["python", "-m", "aep.local_service", "serve"]
