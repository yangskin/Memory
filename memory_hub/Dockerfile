FROM python:3.11-slim
WORKDIR /app
RUN useradd --create-home --uid 10001 memoryhub
COPY pyproject.toml README.md ./
COPY src ./src
COPY alembic.ini ./
COPY migrations ./migrations
RUN pip install --no-cache-dir .
USER memoryhub
EXPOSE 8000
CMD ["memory-hub-api"]