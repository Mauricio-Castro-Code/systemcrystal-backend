FROM python:3.11-slim

RUN echo "deb http://deb.debian.org/debian bookworm main contrib non-free" \
       > /etc/apt/sources.list.d/contrib.list \
    && echo "ttf-mscorefonts-installer msttcorefonts/accepted-mscorefonts-eula select true" \
       | debconf-set-selections \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
       wget \
       ca-certificates \
       libreoffice \
       ttf-mscorefonts-installer \
       locales \
    && echo "es_MX.UTF-8 UTF-8" >> /etc/locale.gen \
    && locale-gen \
    && rm -rf /var/lib/apt/lists/*

ENV LANG=es_MX.UTF-8
ENV LANGUAGE=es_MX:es
ENV LC_ALL=es_MX.UTF-8

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN python manage.py collectstatic --noinput || true

CMD ["gunicorn", "config.wsgi:application", "--config", "gunicorn.conf.py"]
