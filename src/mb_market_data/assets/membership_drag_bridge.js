/* Preview-first row dragging in an explicitly enabled controlled Real Time Overlay.
   Inert in Historical and ordinary read-only Real Time. Drops are advisory:
   no grid rowData mutations, no journal writes, no command confirmation. */
(function () {
    "use strict";

    const TIERS = ["uni", "focus", "hot"];
    const registry = {
        mode: "historical", apis: {}, zones: [], signature: null,
        gesture: null, pointer: null, listeners: []
    };
    window.dash_clientside = window.dash_clientside || {};
    window.dash_clientside.mbMembershipDrag = window.dash_clientside.mbMembershipDrag || {};

    function notify(phase, reason) {
        if (registry.mode !== "real-time") { return; }
        window.dash_clientside.set_props("membership-drag-diagnostic", {
            data: {phase, reason, nonce: Date.now() + "-" + Math.random()}
        });
    }

    // AG Grid emits dragStopped even when a row is released outside its
    // original grid. Capture the pointer to identify an external tier there.
    // This is a fallback only; native onDragStop remains the first choice.
    document.addEventListener("pointermove", event => {
        if (registry.gesture) {
            registry.pointer = {x: event.clientX, y: event.clientY};
        }
    }, {capture: true, passive: true});
    document.addEventListener("mousemove", event => {
        if (registry.gesture) {
            registry.pointer = {x: event.clientX, y: event.clientY};
        }
    }, {capture: true, passive: true});

    function finishDrop(source, target, symbol) {
        if (registry.mode !== "real-time") { return; }
        if (!TIERS.includes(source) || !TIERS.includes(target) || source === target
                || !/^[A-Z0-9.-]{1,20}$/.test(symbol)) { return; }
        if (registry.gesture?.accepted) { return; }
        if (registry.gesture &&
                (registry.gesture.source !== source || registry.gesture.symbol !== symbol)) {
            notify("drop-rejected", "Dragged symbol no longer matches source. Retry.");
            return;
        }
        if (!registry.gesture) {
            registry.gesture = {source, symbol, accepted: false};
        }
        registry.gesture.accepted = true;
        const request = {
            symbol, source_tier: source, destination_tier: target,
            nonce: Date.now() + "-" + Math.random().toString(36).slice(2)
        };
        notify("drop-received", symbol + ": preview requested for " + target.toUpperCase());
        // This writes only a browser Store for server-side Preview validation.
        // It NEVER sends Confirm, adds rows or edits the journal.
        window.dash_clientside.set_props("membership-drag-request", {data: request});
        document.getElementById("operator-controls-panel")
            ?.scrollIntoView({behavior: "smooth", block: "nearest"});
    }

    function removeZones() {
        for (const entry of registry.listeners) {
            try { entry.api.removeEventListener(entry.type, entry.listener); }
            catch (_error) { /* disposed */ }
        }
        registry.listeners = [];
        registry.gesture = null;
        registry.pointer = null;
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
                const nodes = Array.isArray(params?.nodes)
                    ? params.nodes
                    : params?.node ? [params.node] : [];
                if (nodes.length !== 1) {
                    notify("drop-rejected", "Select one symbol per drag.");
                    return;
                }
                const symbol = String(nodes[0]?.data?.symbol || "").trim().toUpperCase();
                finishDrop(source, target, symbol);
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
                const api = apis[tier];
                const onStart = params => {
                    const symbol = String(params?.node?.data?.symbol || "")
                        .trim().toUpperCase();
                    if (registry.mode !== "real-time" || !/^[A-Z0-9.-]{1,20}$/.test(symbol)) {
                        return;
                    }
                    if (!registry.gesture || registry.gesture.accepted
                            || registry.gesture.symbol !== symbol
                            || registry.gesture.source !== tier) {
                        registry.gesture = {source: tier, symbol, accepted: false};
                        notify("drag-started", symbol + ": drop onto another tier list.");
                    }
                };
                const onStopped = () => {
                    const gesture = registry.gesture;
                    if (!gesture || gesture.source !== tier) { return; }
                    if (gesture.accepted) {
                        registry.gesture = null;
                        return;
                    }
                    const pointer = registry.pointer;
                    const element = pointer
                        ? document.elementFromPoint(pointer.x, pointer.y) : null;
                    const panel = element?.closest?.(".membership-tier");
                    const target = TIERS.find(name => panel?.id === "membership-tier-" + name);
                    if (target && target !== tier) {
                        finishDrop(tier, target, gesture.symbol);
                    } else {
                        notify("drop-missed",
                            "Drop not registered. Release over another tier list, then retry.");
                    }
                    registry.gesture = null;
                };
                api.addEventListener("rowDragEnter", onStart);
                api.addEventListener("dragStopped", onStopped);
                registry.listeners.push(
                    {api, type: "rowDragEnter", listener: onStart},
                    {api, type: "dragStopped", listener: onStopped}
                );
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
