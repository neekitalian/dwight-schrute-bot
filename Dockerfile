FROM python:3.12-slim
WORKDIR /app
COPY . .
RUN pip install --no-cache-dir . && useradd --create-home dwight && mkdir /app/runs && chown dwight /app/runs
USER dwight
ENTRYPOINT ["dwight"]
CMD ["strategies"]
