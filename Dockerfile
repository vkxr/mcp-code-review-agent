FROM python:3.11-slim

# git is needed when the test runner checks out pull requests
RUN apt-get update && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

RUN useradd --create-home --uid 1000 reviewer
WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .

ENV REVIEW_DB=/data/reviews.sqlite \
    REVIEW_WORKDIR=/tmp/review-workspaces \
    PORT=8000
RUN mkdir -p /data /tmp/review-workspaces && chown -R reviewer /data /tmp/review-workspaces
USER reviewer

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')"
CMD ["python", "-m", "review_agent.server"]
