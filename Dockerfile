FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /srv
RUN apt-get update && apt-get install -y --no-install-recommends fonts-dejavu-core && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# ASTAP command-line plate solver (star database lives in the data volume: /data/astap)
ADD https://sourceforge.net/projects/astap-program/files/linux_installer/astap_command-line_version_Linux_amd64.zip/download /tmp/astap.zip
RUN python -c "import zipfile;zipfile.ZipFile('/tmp/astap.zip').extractall('/opt/astap')" \
    && chmod +x /opt/astap/astap_cli && rm /tmp/astap.zip
# OpenNGC deep-sky catalogue (CC-BY-SA 4.0, https://github.com/mattiaverga/OpenNGC) for objects in the field
ADD https://raw.githubusercontent.com/mattiaverga/OpenNGC/v20260501/database_files/NGC.csv /opt/openngc/NGC.csv
ADD https://raw.githubusercontent.com/mattiaverga/OpenNGC/v20260501/database_files/addendum.csv /opt/openngc/addendum.csv
COPY app ./app
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
