/* NoiseNet preset for the storage schema of MQTTX Web v1.13.2. */
(() => {
  try {
    const marker = "noisenet-defaults-v1";
    if (localStorage.getItem(marker)) return;

    const db = JSON.parse(localStorage.getItem("db") || "{}");
    const connections = db.connections || [];
    if (!Array.isArray(connections)) throw new Error("Invalid MQTTX connection storage");

    // An existing NoiseNet connection belongs to the user; keep its settings.
    if (!connections.some((connection) => connection.name === "NoiseNet")) {
      const now = new Date().toISOString();
      const random = new Uint32Array(2);
      crypto.getRandomValues(random);
      connections.push({
        id: "noisenet-default",
        name: "NoiseNet",
        host: window.location.hostname,
        port: 8083,
        protocol: "ws",
        path: "/mqtt",
        mqttVersion: "3.1.1",
        clientId: `noisenet-web-${Array.from(random, (n) => n.toString(16)).join("")}`,
        clientIdWithTime: true,
        clean: true,
        keepalive: 60,
        connectTimeout: 10,
        reconnect: true,
        reconnectPeriod: 4000,
        username: "",
        password: "",
        ssl: false,
        certType: "",
        ca: "",
        cert: "",
        key: "",
        rejectUnauthorized: true,
        ALPNProtocols: "",
        createAt: now,
        updateAt: now,
        unreadMessageCount: 0,
        client: { connected: false },
        messages: [],
        pushProps: {},
        properties: {},
        will: {
          lastWillTopic: "",
          lastWillPayload: "",
          lastWillQos: 0,
          lastWillRetain: false,
          properties: {},
        },
        subscriptions: [{
          id: "noisenet-all",
          topic: "noisenet/#",
          qos: 1,
          disabled: false,
          color: "#34c388",
          createAt: now,
        }],
      });
      db.connections = connections;
      localStorage.setItem("db", JSON.stringify(db));
    }
    localStorage.setItem(marker, "1");
  } catch (error) {
    // Storage may be disabled or contain invalid data. Let MQTTX handle its UI.
    console.warn("Could not initialize the NoiseNet MQTTX preset:", error);
  }
})();
