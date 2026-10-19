# Rootless baseline-билдер keklib-cli linux-x86_64 (задача 11, native-продолжение).
#
# Ubuntu 22.04 (glibc 2.35) — закреплённый baseline сборки; образ всегда
# адресуется digest'ом из packaging/builder/builder-pins.json, а не тегом.
# Внутри образа собираются:
#   - SQLite 3.54.1 из upstream-исходника sqlite.org (--enable-fts5);
#   - CPython 3.14.8 из upstream-исходника python.org (static libpython,
#     --without-readline: GPL-компоненты readline/ncurses не входят в
#     runtime; --disable-test-modules; gdbm/tk не устанавливаются вовсе);
#   - /opt/venv со сборочными зависимостями из packaging/requirements-build.lock
#     (==-пины + SHA-256 дистрибутивов).
# Все хеши и версии проверяются при сборке образа (sha256sum -c и asserts
# на версии Python/SQLite/FTS5) — образ не собирается из несовпавшего ввода.
#apt-срез фиксируется снимком dpkg в /opt/builder/apt-versions.txt.

ARG BASE_IMAGE=docker.io/library/ubuntu@sha256:98510ac07bb4545e389be502a8918b385c018bfe4ceeb5ac7e0375c782268b04
FROM ${BASE_IMAGE}

ARG CPYTHON_URL=https://www.python.org/ftp/python/3.14.8/Python-3.14.8.tgz
ARG CPYTHON_SHA256=a65b20a728f169f4e66ae143f40b1bd3d33c38d770251663f627c9767b79b210
ARG CPYTHON_VERSION=3.14.8
ARG SQLITE_URL=https://www.sqlite.org/2026/sqlite-autoconf-3540100.tar.gz
ARG SQLITE_SHA256=926ef8d3eab93fbf6e06011901273eec076b25594a4e1cdadd50c987f4f6d64e
ARG SQLITE_VERSION=3.54.1

ENV DEBIAN_FRONTEND=noninteractive \
    LC_ALL=C.UTF-8

# Сборочные пакеты baseline; libreadline/libgdbm/libncurses/libtk намеренно
# отсутствуют (лицензионное закрытие и минимальный состав runtime).
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        build-essential pkg-config ca-certificates curl \
        libssl-dev zlib1g-dev libffi-dev libbz2-dev liblzma-dev; \
    mkdir -p /opt/builder /opt/src; \
    dpkg-query -W -f='${Package}\t${Version}\n' > /opt/builder/apt-versions.txt

# SQLite из upstream с проверкой хеша (FTS5 обязателен для поискового индекса).
RUN set -eux; \
    curl -fsSL -o /opt/src/sqlite.tar.gz "${SQLITE_URL}"; \
    echo "${SQLITE_SHA256}  /opt/src/sqlite.tar.gz" | sha256sum -c -; \
    tar -xzf /opt/src/sqlite.tar.gz -C /opt/src; \
    cd /opt/src/sqlite-autoconf-*; \
    ./configure --prefix=/opt/sqlite --enable-fts5; \
    make -j"$(nproc)"; \
    make install; \
    echo "/opt/sqlite/lib" > /etc/ld.so.conf.d/keklib-sqlite.conf; \
    ldconfig

# CPython из upstream с проверкой хеша; libpython — общая (--enable-shared:
# обязательное требование PyInstaller 6.x), readline отключён явно,
# тестовые модули не собираются.
RUN set -eux; \
    curl -fsSL -o /opt/src/cpython.tgz "${CPYTHON_URL}"; \
    echo "${CPYTHON_SHA256}  /opt/src/cpython.tgz" | sha256sum -c -; \
    tar -xzf /opt/src/cpython.tgz -C /opt/src; \
    cd "/opt/src/Python-${CPYTHON_VERSION}"; \
    PKG_CONFIG_PATH=/opt/sqlite/lib/pkgconfig \
        ./configure --prefix=/opt/python \
            --enable-shared \
            --without-readline \
            --disable-test-modules \
            --with-ensurepip=install; \
    make -j"$(nproc)"; \
    make install; \
    echo "/opt/python/lib" >> /etc/ld.so.conf.d/keklib-sqlite.conf; \
    ldconfig

# Проверка собранного runtime: точные версии, FTS5, ctypes; readline/dbm/tk
# отсутствуют; libpython общая (--enable-shared — требование PyInstaller).
COPY builder/verify_runtime.py /opt/builder/verify_runtime.py
RUN set -eux; \
    /opt/python/bin/python3 /opt/builder/verify_runtime.py

# Сборочный venv: зависимости только из lock-файла с проверкой хешей.
COPY requirements-build.lock /opt/builder/requirements-build.lock
RUN set -eux; \
    /opt/python/bin/python3 -m venv /opt/venv; \
    /opt/venv/bin/pip install --no-cache-dir \
        -r /opt/builder/requirements-build.lock; \
    /opt/venv/bin/pip list --format=freeze > /opt/builder/pip-versions.txt; \
    /opt/venv/bin/python -c "import PyInstaller, sys; print(sys.version.split()[0], PyInstaller.__version__)"

ENV PATH=/opt/venv/bin:/opt/python/bin:$PATH
WORKDIR /work
