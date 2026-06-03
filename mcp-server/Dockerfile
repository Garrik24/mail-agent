FROM python:3.12-slim

WORKDIR /app

# Системные библиотеки для WeasyPrint (рендер письма HTML -> PDF).
# Активный билдер на Railway — NIXPACKS (см. railway.toml / nixpacks.toml);
# зависимости продублированы здесь, чтобы сборка работала и через Docker.
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpango-1.0-0 \
    libpangoft2-1.0-0 \
    libharfbuzz-subset0 \
    libpangocairo-1.0-0 \
    libgdk-pixbuf-2.0-0 \
    libcairo2 \
    libffi-dev \
    shared-mime-info \
    fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["python", "main.py"]
