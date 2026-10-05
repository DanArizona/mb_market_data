(function () {
    "use strict";

    const chartSelector = "#observation-overlay-chart .js-plotly-plot";
    const boundGraphs = new WeakSet();
    let shiftHeld = false;
    let lastPointer = null;

    function setShift(held) {
        if (shiftHeld === held) return;
        shiftHeld = held;
        if (window.dash_clientside && window.dash_clientside.set_props) {
            window.dash_clientside.set_props("observation-overlay-shift-hover", {
                data: held
            });
        }
    }

    function refreshStationaryHover(graph) {
        if (!lastPointer || lastPointer.graph !== graph) return;
        requestAnimationFrame(function () {
            const target = document.elementFromPoint(lastPointer.x, lastPointer.y);
            if (!target || !graph.contains(target)) return;
            target.dispatchEvent(new MouseEvent("mousemove", {
                bubbles: true,
                clientX: lastPointer.x,
                clientY: lastPointer.y,
                shiftKey: shiftHeld
            }));
        });
    }

    function attachGraph() {
        const graph = document.querySelector(chartSelector);
        if (!graph || boundGraphs.has(graph) || typeof graph.on !== "function") return;
        boundGraphs.add(graph);
        graph.addEventListener("mousemove", function (event) {
            if (!event.isTrusted) return;
            lastPointer = { graph: graph, x: event.clientX, y: event.clientY };
            setShift(event.shiftKey);
        });
        graph.addEventListener("mouseleave", function () {
            if (lastPointer && lastPointer.graph === graph) lastPointer = null;
        });
        graph.on("plotly_afterplot", function () {
            refreshStationaryHover(graph);
        });
    }

    document.addEventListener("keydown", function (event) {
        if (event.key === "Shift") setShift(true);
    });
    document.addEventListener("keyup", function (event) {
        if (event.key === "Shift") setShift(false);
    });
    window.addEventListener("blur", function () { setShift(false); });

    const observer = new MutationObserver(attachGraph);
    observer.observe(document.documentElement, { childList: true, subtree: true });
    attachGraph();
}());
