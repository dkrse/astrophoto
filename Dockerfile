# Builds without internet access: everything the image needs is in vendor/ (see vendor/README.md;
# scripts/fetch-vendor.sh fills it once, online). Only the base image must be in the local Docker cache.
FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /srv
# fonts for the annotated JPEG (DejaVu)
COPY vendor/fonts/ /usr/share/fonts/truetype/dejavu/
# Python packages from local wheels
COPY requirements.txt .
COPY vendor/wheels/ /tmp/wheels/
RUN pip install --no-cache-dir --no-index --find-links /tmp/wheels -r requirements.txt && rm -rf /tmp/wheels
# ASTAP command-line plate solver (star database lives in the data volume: /data/astap)
COPY vendor/astap/astap_cli /opt/astap/astap_cli
RUN chmod +x /opt/astap/astap_cli
# catalogues for objects in the field:
#   OpenNGC (CC-BY-SA 4.0) · HYG v4.1 stars (CC-BY-SA 4.0) · Stellarium DSO v3.21 + names (GPL-2.0+)
COPY vendor/openngc/ /opt/openngc/
COPY vendor/hyg/ /opt/hyg/
COPY vendor/stellarium/ /opt/stellarium/
COPY app ./app
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
