/**
 * Cloudflare Worker: Binance API Reverse Proxy
 */

export default {
  async fetch(request, env, ctx) {
    if (request.method === "OPTIONS") {
      return new Response(null, {
        headers: {
          "Access-Control-Allow-Origin": "*",
          "Access-Control-Allow-Methods": "GET, POST, PUT, DELETE, OPTIONS",
          "Access-Control-Allow-Headers": "*, X-MBX-APIKEY, X-Worker-Auth, Authorization, Content-Type",
        },
      });
    }

    const url = new URL(request.url);

    // При открытии корня в браузере показываем статус работы прокси
    if (url.pathname === "/" || url.pathname === "") {
      return new Response(
        JSON.stringify({
          status: "online",
          service: "Binance API Proxy",
          auth_required: Boolean(env && env.WORKER_AUTH_TOKEN),
          test_endpoints: [
            "/api/v3/ping",
            "/api/v3/time",
            "/api/v3/ticker/price?symbol=BTCUSDT"
          ]
        }, null, 2),
        {
          headers: { "Content-Type": "application/json; charset=utf-8" }
        }
      );
    }

    // Проверка авторизации, если в настройках Cloudflare Worker задан WORKER_AUTH_TOKEN
    const expectedAuth = env && env.WORKER_AUTH_TOKEN ? String(env.WORKER_AUTH_TOKEN).trim() : null;
    if (expectedAuth) {
      const incomingAuth = (request.headers.get("x-worker-auth") || "").trim() ||
        (request.headers.get("authorization") || "").replace(/^Bearer\s+/i, "").trim();
      if (!incomingAuth || incomingAuth !== expectedAuth) {
        return new Response(
          JSON.stringify({ error: "Unauthorized: Invalid or missing X-Worker-Auth token" }),
          {
            status: 401,
            headers: {
              "Content-Type": "application/json",
              "Access-Control-Allow-Origin": "*",
            },
          }
        );
      }
    }

    // Официальный альтернативный кластер Binance (api1/api3)
    let targetHost = "api1.binance.com";
    if (url.pathname.startsWith("/fapi/")) {
      targetHost = "fapi.binance.com";
    }

    const targetUrl = new URL(url.pathname + url.search, `https://${targetHost}`);

    const headers = new Headers();
    for (const [key, value] of request.headers.entries()) {
      const lower = key.toLowerCase();
      if (
        lower === "x-mbx-apikey" ||
        lower === "content-type" ||
        lower === "accept" ||
        lower === "cache-control"
      ) {
        headers.set(key, value);
      }
    }

    // Подставляем стандартный браузерный User-Agent, чтобы CloudFront WAF не блокировал запрос
    const incomingUa = request.headers.get("user-agent");
    headers.set(
      "User-Agent",
      incomingUa && !incomingUa.toLowerCase().includes("cloudflare")
        ? incomingUa
        : "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    );

    const init = {
      method: request.method,
      headers: headers,
      redirect: "follow",
    };

    if (request.method !== "GET" && request.method !== "HEAD") {
      init.body = request.body;
    }

    try {
      const response = await fetch(targetUrl.toString(), init);

      const responseHeaders = new Headers(response.headers);
      responseHeaders.set("Access-Control-Allow-Origin", "*");
      responseHeaders.set("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS");
      responseHeaders.set("Access-Control-Allow-Headers", "*");

      return new Response(response.body, {
        status: response.status,
        statusText: response.statusText,
        headers: responseHeaders,
      });
    } catch (err) {
      return new Response(JSON.stringify({ error: err.message }), {
        status: 502,
        headers: { "Content-Type": "application/json" },
      });
    }
  },
};
