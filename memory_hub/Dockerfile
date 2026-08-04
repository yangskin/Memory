FROM python:3.11-slim
ARG PIP_INDEX_URL=https://pypi.org/simple
ENV PIP_INDEX_URL=${PIP_INDEX_URL} \
	PIP_RETRIES=5 \
	PIP_TIMEOUT=120
WORKDIR /app
RUN useradd --create-home --uid 10001 memoryhub
COPY pyproject.toml README.md ./
COPY src ./src
COPY web ./web
COPY alembic.ini ./
COPY migrations ./migrations
RUN pip install --no-cache-dir .
USER memoryhub
EXPOSE 8000
CMD ["memory-hub-api"]