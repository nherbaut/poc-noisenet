FROM emqx/mqttx-web:v1.13.2

# Seed browser storage before MQTTX reads it. Keep the upstream application intact.
COPY defaults.js /app/noisenet-defaults.js
RUN sed -i 's|<head>|<head><script src="/noisenet-defaults.js"></script>|' /app/index.html
