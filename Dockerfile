FROM python:3.9.4-alpine

ENV CRYPTOGRAPHY_DONT_BUILD_RUST=1
ENV TZ=Asia/Shanghai
ENV FLASK_APP=/opt/azure/app.py
ENV AZURE_MANAGER_DATABASE_URI=sqlite:////root/azure/database.db
WORKDIR /root/azure
COPY requirements.txt /tmp/requirements.txt
RUN apk --no-cache add tzdata gcc g++ libffi-dev libressl-dev &&\
    pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r /tmp/requirements.txt &&\
    apk del gcc g++ libffi-dev libressl-dev
COPY azure /opt/azure

CMD ["sh", "-c", "flask initdb && python /opt/azure/app.py"]
