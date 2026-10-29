# MillieBeanCounter: Goddard/Kaymbu daycare emails in Gmail -> Huckleberry and Google Drive.
# No Android emulator, ADB or OCR: entries go straight in through py-huckleberry-api.
#
# Two stages. The first starts from the official Python image, applies Debian's
# security updates, installs the libraries, then removes everything the sync never
# uses (pip, and Debian tools like the shell, apt and tar; see tools/slim_image.py),
# so the vulnerabilities in them leave too. The second copies what's left into an
# empty image, so no removed file survives in a lower layer, and runs every test
# against that final image. The build stops if a test fails.

FROM python:3.14-slim AS build

ARG DEBIAN_FRONTEND=noninteractive
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_ROOT_USER_ACTION=ignore \
    PYTHONDONTWRITEBYTECODE=1

# Debian security fixes released since the Python image was made (every build).
RUN apt-get update \
    && apt-get -y upgrade \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd --gid 10001 millie \
    && useradd --uid 10001 --gid 10001 --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin millie \
    && mkdir -p /data \
    && chown millie:millie /data \
    && chmod 700 /data

WORKDIR /app
COPY requirements.txt constraints.txt ./
RUN pip install --only-binary=:all: -r requirements.txt -c constraints.txt

COPY tools/slim_image.py /tmp/slim_image.py
RUN ["python", "/tmp/slim_image.py", "--docker-build"]


FROM scratch

COPY --from=build / /
ENV PATH=/usr/local/bin:/usr/local/sbin:/usr/sbin:/usr/bin:/sbin:/bin \
    LANG=C.UTF-8 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    MILLIE_DATA=/data \
    TZ=America/New_York \
    GRPC_VERBOSITY=ERROR

# The settings volume is created from /data, so it must belong to the sync's user;
# /tmp must stay writable for everyone.
RUN ["python", "-c", "import os; os.chown('/data', 10001, 10001); os.chmod('/data', 0o700); os.chmod('/tmp', 0o1777)"]

WORKDIR /app
COPY millie_bean_counter ./millie_bean_counter
COPY tests ./tests
COPY tools ./tools

USER 10001:10001
# The build stops here if anything is off. The tests read made-up daycare emails and
# push them through the real Huckleberry library into a fake database.
RUN ["python", "-m", "unittest", "discover", "-s", "tests"]

ENTRYPOINT ["python", "-m", "millie_bean_counter"]
CMD ["serve"]
