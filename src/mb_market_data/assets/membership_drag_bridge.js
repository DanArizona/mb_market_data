/* Preview-first row dragging in an explicitly enabled controlled Real Time Overlay.
   Inert in Historical and ordinary read-only Real Time. Drops are advisory:
   no grid rowData mutations, no journal writes, no command confirmation. */
(function () {
    "use strict";

    const TIERS = ["uni", "focus", "hot"];
    const registry = { mode: "historical", apis: {}, zones: [], signature: null };
    window.dash_clientside = window.dash_clientside || {};
    window.dash_clientside.mbMembershipDrag = window.dash_clientside.mbMembershipDrag || {};

    function removeZones() {
        for (const entry of registry.zones) {
            try { entry.api.removeRowDropZone(entry.params); } catch (_error) { /* old grid disposed */ }
        }
        registry.zones = [];
        for (const tier of TIERS) {
            document.getElementById("membership-tier-" + tier)
                ?.classList.remove("membership-drop-target", "membership-drop-ready");
        }
    }

    function suppressAll() {
        for (const api of Object.values(registry.apis)) {
            try {
                api.setGridOption("suppressRowDrag", true);
                if (typeof api.refreshCells === "function") { api.refreshCells({force: true}); }
            } catch (_error) { /* disposed */ }
        }
    }

    function makeZone(source, target, api) {
        const panel = document.getElementById("membership-tier-" + target);
        if (!panel) { throw new Error("Target membership panel is missing"); }
        return {
            getContainer: () => panel,
            onDragEnter: () => {
                if (registry.mode === "real-time") {
                    panel.classList.add("membership-drop-target");
                }
            },
            onDragLeave: () => panel.classList.remove("membership-drop-target"),
            onDragStop: (params) => {
                panel.classList.remove("membership-drop-target");
                if (registry.mode !== "real-time") { return; }
                const nodes = Array.isArray(params?.nodes)
                    ? params.nodes
                    : params?.node ? [params.node] : [];
                if (nodes.length !== 1) { return; } // Single-symbol actions only.
                const symbol = String(nodes[0]?.data?.symbol || "").trim().toUpperCase();
                if (!/^[A-Z0-9.-]{1,20}$/.test(symbol)) { return; }
                const request = {
                    symbol: symbol,
                    source_tier: source,
                    destination_tier: target,
                    nonce: Date.now() + "-" + Math.random().toString(36).slice(2)
                };
                // The server re-reads the effective tier and only PREVIEWS.
                // No client-side list mutation, membership inbox write, or confirmation.
                window.dash_clientside.set_props("membership-drag-request", {data: request});
                document.getElementById("operator-controls-panel")
                    ?.scrollIntoView({behavior: "smooth", block: "nearest"});
            }
        };
    }

    window.dash_clientside.mbMembershipDrag.bind = async function (mode, uniId, focusId, hotId) {
        const enabled = mode === "real-time";
        registry.mode = enabled ? "real-time" : "historical";
        if (!enabled) {
            removeZones();
            suppressAll();
            registry.signature = null;
            return {ready: false, phase: "historical-read-only"};
        }
        try {
            if (!window.dash_ag_grid || typeof window.dash_ag_grid.getApiAsync !== "function") {
                throw new Error("Dash AG Grid API is unavailable");
            }
            const ids = [uniId, focusId, hotId];
            if (ids.some(id => typeof id !== "string" || !id)) {
                throw new Error("Membership grids are not ready");
            }
            // Wait for Dash/React to mount all three AG Grid instances.
            const activeApis = await Promise.all(ids.map(id => window.dash_ag_grid.getApiAsync(id)));
            if (registry.mode !== "real-time") {
                return {ready: false, phase: "historical-read-only"};
            }
            const apis = Object.fromEntries(TIERS.map((tier, i) => [tier, activeApis[i]]));
            const same = registry.signature === ids.join(":")
                && TIERS.every(tier => registry.apis[tier] === apis[tier])
                && registry.zones.length === 6;
            if (same) { return {ready: true, phase: "controlled-drag-enabled"}; }
            removeZones();
            suppressAll();
            registry.apis = apis;
            // Generic HTML panel targets do not insert rows in the target grid.
            // Even with sorting enabled, external drop zones remain available.
            for (const source of TIERS) {
                for (const target of TIERS) {
                    if (source === target) { continue; }
                    const zone = makeZone(source, target, apis[source]);
                    apis[source].addRowDropZone(zone);
                    registry.zones.push({api: apis[source], params: zone});
                }
            }
            for (const tier of TIERS) {
                apis[tier].setGridOption("suppressRowDrag", false);
                if (typeof apis[tier].refreshCells === "function") {
                    apis[tier].refreshCells({force: true});
                }
                document.getElementById("membership-tier-" + tier)
                    ?.classList.add("membership-drop-ready");
            }
            registry.signature = ids.join(":");
            return {ready: true, phase: "controlled-drag-enabled"};
        } catch (error) {
            removeZones();
            suppressAll();
            registry.signature = null;
            return {ready: false, phase: "drag-unavailable",
                    reason: error instanceof Error ? error.message : "Grid setup failed"};
        }
    };
})();
