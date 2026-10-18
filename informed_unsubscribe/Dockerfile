FROM alpine:3.22

# Everything comes from Alpine packages: no pip, no compiler, one small layer.
# tzdata: Home Assistant passes its time zone in TZ, which means nothing without the zone files.
RUN apk add --no-cache python3 py3-jinja2 py3-beautifulsoup4 py3-pillow \
    tesseract-ocr tesseract-ocr-data-eng tzdata

WORKDIR /opt/informed_unsubscribe
COPY app ./app

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
CMD ["python3", "-m", "app"]
