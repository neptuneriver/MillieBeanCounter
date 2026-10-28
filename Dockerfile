# Millie daycare sync: Goddard/Kaymbu daily sheets in Gmail -> Huckleberry.
# No Android emulator, ADB or OCR: entries go straight in through py-huckleberry-api.
FROM python:3.14-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_ROOT_USER_ACTION=ignore \
    MILLIE_DATA=/data \
    TZ=America/New_York \
    GRPC_VERBOSITY=ERROR

RUN groupadd --gid 10001 millie \
    && useradd --uid 10001 --gid 10001 --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin millie \
    && mkdir -p /data \
    && chown millie:millie /data \
    && chmod 700 /data

WORKDIR /app
COPY requirements.txt constraints.txt ./
RUN pip install --only-binary=:all: -r requirements.txt -c constraints.txt

COPY millie_sync ./millie_sync
COPY tests ./tests

USER millie
# The build stops here if anything is off. The tests read a copy of a real daily
# sheet and push it through the real Huckleberry library into a fake database.
RUN python -m unittest discover -s tests

ENTRYPOINT ["python", "-m", "millie_sync"]
CMD ["serve"]
