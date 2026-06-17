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

EXPOSE 8282

CMD update-ca-certificates && \
    ln -sf /proc/1/fd/1 /dev/stdout && \
    ln -sf /proc/1/fd/2 /dev/stderr && \
    mkdir /app/data/ && cd /app/data && \
    (python3 -m http.server &) && \
    (stayrtr -bind 0.0.0.0:8282 -cache http://localhost:8000/output.json -checktime=false -refresh $INTERVAL &) && \
    cd /app && \
    python3 -u client.py
