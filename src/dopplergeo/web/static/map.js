// US basemap built only from bundled data (no tiles unless the user opts in), plus helpers for
// the region picker, result overlays and dataset previews.
(function () {
  'use strict';
  const STATIC = '/static/vendor/';
  let atlasPromise = null, placesPromise = null;
  const css = name => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

  function loadAtlas() {
    atlasPromise = atlasPromise || fetch(STATIC + 'us-counties-10m.json').then(r => r.json()).then(us => ({
      nation: topojson.feature(us, us.objects.nation),
      states: topojson.mesh(us, us.objects.states, (a, b) => a !== b),
      counties: topojson.mesh(us, us.objects.counties, (a, b) => a !== b && ((a.id / 1000) | 0) === ((b.id / 1000) | 0)),
    }));
    return atlasPromise;
  }
  function loadPlaces() {
    placesPromise = placesPromise || fetch(STATIC + 'us-places.json').then(r => r.json());
    return placesPromise;
  }

  function popThreshold(z) {
    if (z < 4.5) return 3e6; if (z < 5.5) return 1e6; if (z < 6.5) return 3e5;
    if (z < 7.5) return 8e4; if (z < 8.5) return 2e4; return 0;
  }

  function create(el, opts = {}) {
    const map = L.map(el, {preferCanvas: true, zoomSnap: 0.25, worldCopyJump: false, attributionControl: true,
                           minZoom: 3, maxZoom: 16});
    map.attributionControl.setPrefix(false);
    map.attributionControl.addAttribution('US Census Bureau · Natural Earth');
    map.setView(opts.center || [39.5, -97.5], opts.zoom || 4);
    const panes = ['base', 'lines', 'data', 'top'];
    panes.forEach((p, i) => { map.createPane(p); map.getPane(p).style.zIndex = 300 + i * 50; });
    map.getPane('base').style.pointerEvents = 'none';
    map.getPane('lines').style.pointerEvents = 'none';

    // colours come from the map element's own CSS variables, so a container (e.g. the dark live panel) can restyle it
    const cs = getComputedStyle(el), cv = n => cs.getPropertyValue(n).trim() || css(n);
    const land = cv('--map-land'), line = cv('--map-line'), cline = cv('--map-county');
    loadAtlas().then(a => {
      L.geoJSON(a.nation, {pane: 'base', interactive: false,
        style: {color: line, weight: 1.2, fillColor: land, fillOpacity: 1}}).addTo(map);
      const counties = L.geoJSON(a.counties, {pane: 'lines', interactive: false, style: {color: cline, weight: 0.6}});
      L.geoJSON(a.states, {pane: 'lines', interactive: false, style: {color: line, weight: 1.1}}).addTo(map);
      const sync = () => { if (map.getZoom() >= 6) counties.addTo(map); else counties.remove(); };
      map.on('zoomend', sync); sync();
    });

    // city labels, density by zoom
    const placeLayer = L.layerGroup().addTo(map);
    loadPlaces().then(p => {
      const draw = () => {
        placeLayer.clearLayers();
        const b = map.getBounds().pad(0.1), th = popThreshold(map.getZoom());
        let n = 0;
        for (const [name, lat, lon, pop] of p.places) {
          if (pop < th || !b.contains([lat, lon])) continue;
          L.circleMarker([lat, lon], {pane: 'top', radius: 2.5, weight: 0, fillColor: cv('--map-place'), fillOpacity: 1,
                                      interactive: false})
            .bindTooltip(name, {permanent: true, direction: 'right', className: 'placelabel', offset: [4, 0]})
            .addTo(placeLayer);
          if (++n > 120) break;
        }
      };
      map.on('moveend', draw); draw();
    });

    // opt-in street map
    const osm = L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
      maxZoom: 19, attribution: '© OpenStreetMap contributors', pane: 'base', opacity: 0.85});
    const Toggle = L.Control.extend({
      onAdd() {
        const d = L.DomUtil.create('label', 'leaflet-bar maptoggle');
        d.innerHTML = '<input type="checkbox"> Street map';
        d.title = 'Loads OpenStreetMap tiles: openstreetmap.org sees which area you are viewing.';
        L.DomEvent.disableClickPropagation(d);
        d.querySelector('input').addEventListener('change', e => {
          if (e.target.checked) {
            if (!sessionStorageGet('osm-ok') && !confirm('Street map tiles come from openstreetmap.org, which will see the ' +
                'area you are viewing (e.g. your recording location). Turn it on?')) { e.target.checked = false; return; }
            sessionStorageSet('osm-ok', '1'); osm.addTo(map);
          } else osm.remove();
        });
        return d;
      }
    });
    new Toggle({position: 'topright'}).addTo(map);
    L.control.scale({imperial: true, metric: true}).addTo(map);
    return map;
  }

  function sessionStorageGet(k) { try { return sessionStorage.getItem(k); } catch (e) { return null; } }
  function sessionStorageSet(k, v) { try { sessionStorage.setItem(k, v); } catch (e) { /* ignore */ } }

  const ramp = t => {
    const stops = [[59, 76, 192], [90, 180, 172], [246, 224, 94], [232, 89, 12]];
    const x = Math.min(Math.max(t, 0), 1) * (stops.length - 1), i = Math.min(Math.floor(x), stops.length - 2), f = x - i;
    const c = stops[i].map((v, k) => Math.round(v + (stops[i + 1][k] - v) * f));
    return `rgb(${c[0]},${c[1]},${c[2]})`;
  };

  // ---------------------------------------------------------------- region picker
  function picker(el) {
    const form = el.closest('form');
    const f = n => form.querySelector(`[name="${n}"]`);
    const latI = f(el.getAttribute('data-lat')), lonI = f(el.getAttribute('data-lon')), rk = el.getAttribute('data-radius-km'), radI = rk ? f(rk) : null;
    const map = create(el);
    let marker = null, circle = null;
    const render = (fit) => {
      const lat = parseFloat(latI.value), lon = parseFloat(lonI.value);
      if (!isFinite(lat) || !isFinite(lon)) return;
      const r = radI ? parseFloat(radI.value) * 1000 : 0;
      if (!marker) { marker = L.marker([lat, lon], {draggable: true, pane: 'top'}).addTo(map);
        marker.on('dragend', () => { const p = marker.getLatLng(); latI.value = p.lat.toFixed(5); lonI.value = p.lng.toFixed(5); render(false); }); }
      marker.setLatLng([lat, lon]);
      if (r > 0) {
        if (!circle) circle = L.circle([lat, lon], {radius: r, pane: 'data', color: css('--accent'), weight: 2, fillOpacity: 0.06, interactive: false}).addTo(map);
        circle.setLatLng([lat, lon]); circle.setRadius(r);
        if (fit) map.fitBounds(circle.getBounds(), {padding: [20, 20], maxZoom: 11});
      } else if (fit) map.setView([lat, lon], 10);
    };
    map.on('click', e => { latI.value = e.latlng.lat.toFixed(5); lonI.value = e.latlng.lng.toFixed(5);
      render(map.getZoom() < 8); });
    [latI, lonI, radI].filter(Boolean).forEach(i => i.addEventListener('change', () => render(true)));
    render(true);
    // Leaflet needs a size recalculation when a hidden tab becomes visible
    document.addEventListener('dopplergeo:tab', () => setTimeout(() => map.invalidateSize(), 50));
    return map;
  }

  // ---------------------------------------------------------------- result overlay
  async function result(el) {
    const r = await fetch(el.dataset.src);
    if (!r.ok) { el.textContent = 'No candidates to show.'; return; }
    const d = await r.json();
    const map = create(el);
    const P = d.points, n = P.lat.length;
    const smin = Math.min(...P.score), smax = Math.max(...P.score);
    const layers = [];
    if (d.region) {
      layers.push(L.circle([d.region.lat, d.region.lon], {radius: d.region.radius_m, pane: 'data', color: css('--muted'),
        weight: 1.5, dashArray: '6 6', fill: false, interactive: false}).addTo(map));
    }
    if (d.tracks) {
      L.geoJSON(d.tracks, {pane: 'data', style: ft => ft.properties.in_best ?
          {color: css('--accent'), weight: 3, opacity: 0.95} : {color: css('--muted'), weight: 1.5, opacity: 0.7},
        onEachFeature: (ft, l) => l.bindTooltip(`${ft.properties.icao24}${ft.properties.type_code ? ' · ' + ft.properties.type_code : ''}` +
          (ft.properties.in_best ? ' · used for #1' : ''), {sticky: true})}).addTo(map);
    }
    const order = [...Array(n).keys()].sort((a, b) => P.score[a] - P.score[b]);
    for (const i of order) {
      const t = (P.score[i] - smin) / ((smax - smin) || 1);
      L.circle([P.lat[i], P.lon[i]], {radius: 55, pane: 'data', stroke: false, fillColor: ramp(t), fillOpacity: 0.85})
        .bindTooltip(`#${P.rank[i]} · ${P.lat[i].toFixed(6)}, ${P.lon[i].toFixed(6)}<br>score ${P.score[i].toFixed(0)} · ${P.combo[i]}`)
        .addTo(map);
    }
    const best = L.circleMarker([P.lat[0], P.lon[0]], {pane: 'top', radius: 11, color: css('--danger'), weight: 3, fill: false})
      .bindTooltip(`best: ${P.lat[0].toFixed(6)}, ${P.lon[0].toFixed(6)}`).addTo(map);
    layers.push(best);
    if (d.truth) {
      layers.push(L.marker([d.truth.lat, d.truth.lon], {pane: 'top', icon: L.divIcon({className: 'truthicon', html: '★', iconSize: [22, 22]})})
        .bindTooltip('known location').addTo(map));
    }
    const b = L.latLngBounds(P.lat.map((v, i) => [v, P.lon[i]]));
    map.fitBounds(d.region ? layers[0].getBounds() : b, {padding: [16, 16]});
    // US overview inset: where in the country this is
    const Inset = L.Control.extend({
      onAdd() {
        const div = L.DomUtil.create('div', 'inset');
        setTimeout(() => {
          const m2 = L.map(div, {zoomControl: false, attributionControl: false, dragging: false, scrollWheelZoom: false,
            doubleClickZoom: false, boxZoom: false, keyboard: false, touchZoom: false});
          m2.setView([38.5, -96.5], 2);
          loadAtlas().then(a => {
            L.geoJSON(a.nation, {style: {color: css('--map-line'), weight: 0.6, fillColor: css('--map-land'), fillOpacity: 1}, interactive: false}).addTo(m2);
            L.geoJSON(a.states, {style: {color: css('--map-line'), weight: 0.4}, interactive: false}).addTo(m2);
            L.circleMarker([P.lat[0], P.lon[0]], {radius: 4, color: css('--danger'), weight: 2, fillOpacity: 1}).addTo(m2);
            m2.fitBounds([[24.5, -125], [49.5, -66.5]]);
          });
        }, 0);
        return div;
      }
    });
    new Inset({position: 'bottomright'}).addTo(map);
  }

  // ---------------------------------------------------------------- dataset preview
  function datamap(el) {
    const map = create(el);
    let layer = null;
    const show = async name => {
      el.closest('.panel').querySelector('#datamap-note').textContent = `Loading ${name}…`;
      const bb = map.getBounds();
      const r = await fetch(`/data/${encodeURIComponent(name)}/tracks.json?bbox=` +
        [bb.getSouth(), bb.getWest(), bb.getNorth(), bb.getEast()].map(v => v.toFixed(3)).join(','));
      const g = await r.json();
      if (g.note) { el.closest('.panel').querySelector('#datamap-note').textContent = g.note; return; }
      if (layer) layer.remove();
      layer = L.geoJSON(g, {pane: 'data', style: {color: css('--accent'), weight: 1.2, opacity: 0.7},
        onEachFeature: (ft, l) => l.bindTooltip(`${ft.properties.icao24}${ft.properties.type_code ? ' · ' + ft.properties.type_code : ''}`, {sticky: true})}).addTo(map);
      if (g.features.length) map.fitBounds(layer.getBounds(), {padding: [10, 10]});
      el.closest('.panel').querySelector('#datamap-note').textContent = g.total_flights > g.features.length ?
        `${name}: ${g.features.length} of ${g.total_flights} flight tracks shown (preview limit).` : `${name}: ${g.features.length} flight tracks shown.`;
    };
    document.querySelectorAll('.preview-ds').forEach(b => b.addEventListener('click', () => show(b.dataset.name)));
    document.querySelectorAll('.use-view').forEach(b => b.addEventListener('click', () => {
      const form = b.closest('form'), set = (n, v) => { const i = form.querySelector(`[name="${n}"]`); if (i) i.value = v; };
      if (b.dataset.target === 'bbox') {
        const bb = map.getBounds();
        set('south', bb.getSouth().toFixed(3)); set('north', bb.getNorth().toFixed(3));
        set('west', bb.getWest().toFixed(3)); set('east', bb.getEast().toFixed(3)); set('region_label', '');
        const rp = form.querySelector('#region-preset'); if (rp) rp.value = '';
        ['south', 'west', 'north', 'east'].forEach(k => { const i = form.querySelector(`[name="${k}"]`); i.disabled = false; i.required = true; });
      } else {
        const c = map.getCenter(); set('lat', c.lat.toFixed(4)); set('lon', c.lng.toFixed(4));
      }
    }));
    document.addEventListener('dopplergeo:tab', () => setTimeout(() => map.invalidateSize(), 50));
  }

  // state presets for the rehydrate box (bbox of each state's outline)
  function presets(sel) {
    fetch(STATIC + 'us-counties-10m.json').then(r => r.json()).then(us => {
      const fc = topojson.feature(us, us.objects.states);
      const states = fc.features.map(f => {
        const b = L.geoJSON(f).getBounds();
        return {name: f.properties.name, b};
      }).sort((a, z) => a.name.localeCompare(z.name));
      // Alaska/territories straddling the antimeridian are skipped (their box would span the globe)
      for (const st of states) {
        if (st.b.getEast() - st.b.getWest() > 60) continue;
        const o = document.createElement('option');
        o.value = [st.b.getSouth(), st.b.getWest(), st.b.getNorth(), st.b.getEast()].map(v => v.toFixed(2)).join(',');
        o.textContent = st.name + ' (whole state)';
        sel.appendChild(o);
      }
      sel.addEventListener('change', () => {
        if (!sel.value) return;
        const form = sel.closest('form'), set = (k, v) => { form.querySelector(`[name="${k}"]`).value = v; };
        const boxIn = ['south', 'west', 'north', 'east'].map(k => form.querySelector(`[name="${k}"]`));
        if (sel.value === 'world') {
          boxIn.forEach(i => { i.value = ''; i.required = false; i.disabled = true; });
          set('region_label', 'Whole world');
          return;
        }
        boxIn.forEach(i => { i.required = true; i.disabled = false; });
        const [s, w, n, e] = sel.value.split(',');
        // pad by 0.2 deg so flights approaching the border are kept
        set('south', (+s - 0.2).toFixed(2)); set('west', (+w - 0.2).toFixed(2));
        set('north', (+n + 0.2).toFixed(2)); set('east', (+e + 0.2).toFixed(2));
        set('region_label', sel.selectedOptions[0].textContent.replace(' (whole state)', ''));
      });
    });
  }

  window.USMap = {create, picker, result, datamap, ramp, presets};
  document.addEventListener('DOMContentLoaded', () => {
    document.querySelectorAll('.pickmap').forEach(picker);
    document.querySelectorAll('.resultmap').forEach(result);
    const dm = document.getElementById('datamap'); if (dm) datamap(dm);
    const rp = document.getElementById('region-preset'); if (rp) presets(rp);
  });
})();
