# Ubuntu, not Debian: bookworm dropped the asterisk package entirely.
FROM ubuntu:24.04

# Asterisk 20, plus the English core sounds used by the test extensions in
# extensions.conf.
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        asterisk \
        asterisk-core-sounds-en-gsm \
    && rm -rf /var/lib/apt/lists/*

# Run in the foreground so Docker owns the process lifecycle.
CMD ["asterisk", "-f", "-vvv"]
