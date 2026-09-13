FROM alpine:3.20

LABEL org.opencontainers.image.title="m4b-faststart" \
      org.opencontainers.image.description="One-shot Docker job that moves the moov atom to the front of .m4b audiobooks (faststart) for instant playback start" \
      org.opencontainers.image.source="https://github.com/hypnotoad08/m4b-faststart" \
      org.opencontainers.image.licenses="MIT"

RUN apk add --no-cache ffmpeg python3

COPY faststart.py /app/faststart.py

WORKDIR /app
VOLUME ["/audiobooks", "/data"]

ENTRYPOINT ["python3", "/app/faststart.py"]
CMD ["/audiobooks"]
