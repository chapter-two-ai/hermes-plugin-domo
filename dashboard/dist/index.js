// Domo admin tab for the Hermes dashboard: each profile's open decisions, runs and today's brief.
(function () {
  "use strict";
  const SDK = window.__HERMES_PLUGIN_SDK__;
  const { React } = SDK;
  const { useState, useEffect } = SDK.hooks;
  const { Card, CardHeader, CardTitle, CardContent, Badge } = SDK.components;
  const h = React.createElement;

  function Profile({ info }) {
    const [feed, setFeed] = useState(null);
    const [error, setError] = useState(null);
    useEffect(() => {
      SDK.fetchJSON("/api/plugins/domo/feed?agent=" + encodeURIComponent(info.id))
        .then(setFeed).catch((e) => setError(String(e)));
    }, [info.id]);
    const counts = feed ? feed.counts : null;
    return h(Card, { className: "mb-4" },
      h(CardHeader, null, h(CardTitle, null, info.display_name + " ",
        counts ? h(Badge, null, counts.needs_you + " need you · " + counts.running + " running") : null)),
      h(CardContent, null,
        error ? h("p", { className: "text-sm text-destructive" }, error) : null,
        feed && feed.brief ? h("p", { className: "text-sm mb-3" }, feed.brief.body) : null,
        feed ? h("ul", { className: "text-sm space-y-1" },
          feed.decisions.map((d) => h("li", { key: d.id }, "◇ " + d.title + "  (" + (d.data.options || []).join(" / ") + ")")),
          feed.runs.map((r) => h("li", { key: r.id }, "▸ " + r.title + " — " + r.status +
            (r.data.progress != null ? " " + Math.round(r.data.progress * 100) + "%" : "")))
        ) : h("p", { className: "text-sm text-muted-foreground" }, "Loading…")));
  }

  function DomoPage() {
    const [profiles, setProfiles] = useState([]);
    useEffect(() => { SDK.fetchJSON("/api/plugins/domo/profiles").then((r) => setProfiles(r.profiles)); }, []);
    return h("div", null, profiles.map((p) => h(Profile, { key: p.id, info: p })));
  }

  window.__HERMES_PLUGINS__.register("domo", DomoPage);
})();
