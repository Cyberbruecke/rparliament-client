FROM ubuntu:latest
#upgrade & python
RUN apt update && apt upgrade -y && apt autoremove -y && apt install -y python3 python3-pip ca-certificates

#stayrtr
COPY --from=rpki/stayrtr /stayrtr /bin/stayrtr

#app
COPY client.py /app/client.py
COPY requirements.txt /app/requirements.txt
COPY rtrdump /app/rtrdump
RUN pip3 install --break-system-packages -r /app/requirements.txt

EXPOSE 8282 9282

CMD cat /usr/local/share/ca-certificates/rparliament.crt >> /etc/ssl/certs/ca-certificates.crt && \
    ln -sf /proc/1/fd/1 /dev/stdout && \
    ln -sf /proc/1/fd/2 /dev/stderr && \
    mkdir -p /app/data/ && cd /app/data && \
    (python3 -m http.server &) && \
    RTR_KEY=/etc/ssl/private/rtr.key && \
    RTR_CERT=/etc/ssl/certs/rtr.crt && \
    if [ -f "$RTR_KEY" ] && [ -f "$RTR_CERT" ]; then \
        echo "TLS cert/key found, serving RTR over TLS only"; \
        (stayrtr -bind "" -tls.bind 0.0.0.0:8282 -tls.cert "$RTR_CERT" -tls.key "$RTR_KEY" -cache http://localhost:8000/output.json -checktime=false -refresh $INTERVAL -metrics.addr :9847 &); \
    else \
        echo "No TLS cert/key, serving RTR in plain mode"; \
        (stayrtr -bind 0.0.0.0:8282 -cache http://localhost:8000/output.json -checktime=false -refresh $INTERVAL -metrics.addr :9847 &); \
    fi && \
    cd /app && \
    python3 -u client.py
