FROM alpine:3.20

RUN apk add --no-cache ffmpeg python3

COPY faststart.py /app/faststart.py

WORKDIR /app
VOLUME ["/audiobooks", "/data"]

ENTRYPOINT ["python3", "/app/faststart.py"]
CMD ["/audiobooks"]
