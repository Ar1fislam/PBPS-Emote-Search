FROM python:3.12-bookworm

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt \
  && python -m playwright install --with-deps chromium

COPY . .

ENV PYTHONUNBUFFERED=1
CMD sh -c "uvicorn main:app --host 0.0.0.0 --port ${PORT}"
