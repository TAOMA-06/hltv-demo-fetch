# hltv-demo-fetch in a small Linux container.
#
#   docker build -t hltv-demo-fetch .
#   docker run --rm -v "$PWD/demos:/demos" hltv-demo-fetch --limit 5
#
# The fast path only needs curl_cffi.  Build with
# `--build-arg WITH_BROWSER=1` to add Playwright + Chromium for the
# Cloudflare managed-challenge fallback (adds ~500 MB).
ARG PYTHON_IMAGE=docker.m.daocloud.io/library/python:3.12-slim
FROM ${PYTHON_IMAGE}

ARG PIP_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple
ARG WITH_BROWSER=0
ARG PLAYWRIGHT_DOWNLOAD_HOST=https://cdn.npmmirror.com/binaries/playwright

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_INDEX_URL=${PIP_INDEX} \
    PLAYWRIGHT_DOWNLOAD_HOST=${PLAYWRIGHT_DOWNLOAD_HOST}

# bsdtar (libarchive) handles RAR5, zip, 7z and tar without extra tools.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ca-certificates \
      libarchive-tools \
      unzip \
 && if [ "$WITH_BROWSER" = "1" ]; then \
      apt-get install -y --no-install-recommends libatomic1 xvfb; \
    fi \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt ./
RUN pip install -r requirements.txt

RUN if [ "$WITH_BROWSER" = "1" ]; then \
      python -m playwright install --with-deps chromium; \
    fi

COPY . .
RUN pip install --no-deps -e .

VOLUME ["/demos"]
WORKDIR /demos

ENTRYPOINT ["python", "-m", "hltv_demo_fetch"]
CMD ["--help"]
