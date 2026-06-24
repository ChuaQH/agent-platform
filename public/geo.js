// Browser geolocation + timezone — timezone sent immediately on load
(function () {
  var timezone = Intl.DateTimeFormat().resolvedOptions().timeZone || "";
  var prompted = false;

  // Send timezone immediately (no permission needed)
  fetch("/api/location", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ lat: 0, lng: 0, timezone: timezone }),
  }).then(function (r) {
    console.log("[geo] initial timezone send:", r.status, "timezone:", timezone);
  }).catch(function (e) {
    console.log("[geo] initial timezone send FAILED:", e);
  });

  function send(lat, lng) {
    fetch("/api/location", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ lat: lat, lng: lng, timezone: timezone }),
    }).catch(function () {});
  }

  function checkAndPrompt() {
    fetch("/api/location/status")
      .then(function (r) { return r.json(); })
      .then(function (data) {
        if (data.pending && !prompted) {
          prompted = true;
          if ("geolocation" in navigator) {
            console.log("[geo] requesting location...");
            navigator.geolocation.getCurrentPosition(
              function (pos) {
                console.log("[geo] granted:", pos.coords.latitude, pos.coords.longitude);
                send(pos.coords.latitude, pos.coords.longitude);
              },
              function (err) {
                console.log("[geo] error code:", err.code, "message:", err.message);
                if (err.code === 1) {
                  send(0, 0);
                } else {
                  prompted = false;
                }
              },
              { timeout: 120000, maximumAge: 0, enableHighAccuracy: false }
            );
          }
        }
        if (!data.pending) {
          prompted = false;
        }
      })
      .catch(function (e) {
        console.log("[geo] status fetch error:", e);
      });
  }

  setInterval(checkAndPrompt, 1500);
  checkAndPrompt();
})();
