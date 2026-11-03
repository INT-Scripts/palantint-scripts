// Paste into the browser console while signed in on portal.bananint.fr.
(async () => {
    const token = localStorage.getItem('palantint_token');
    if (!token) throw new Error('Connectez-vous avant de lancer cet export.');
    const read = async path => {
        for (const prefix of ['/api/private', '/api']) {
            const response = await fetch(prefix + path, {headers: {Authorization: `Bearer ${token}`}});
            if (response.status === 404) continue;
            if (!response.ok) throw new Error(`${path}: HTTP ${response.status}. Reconnectez-vous si nécessaire.`);
            return response.json();
        }
        throw new Error(`Route indisponible : ${path}`);
    };
    const config = await read('/maps/3d-config');
    const buildings = await read('/maps/buildings');
    const maps = [];
    for (const [building, floors] of Object.entries(buildings)) {
        for (const floor of floors) {
            const value = await read(`/maps/${encodeURIComponent(building)}/${encodeURIComponent(floor)}/metadata`);
            if (value.pillars?.length) maps.push({building_id: building, floor_id: String(floor), pillars: value.pillars});
        }
    }
    const bundle = {schema_version: 1, source: location.origin, exported_at: new Date().toISOString(), config, maps};
    const url = URL.createObjectURL(new Blob([JSON.stringify(bundle, null, 2)], {type: 'application/json'}));
    const link = document.createElement('a');
    link.href = url; link.download = 'palantint-3d-alignments.json'; link.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
    console.info(`Export terminé : ${config.markers.length} bâtiments, ${maps.length} calibrations d’étage.`);
})();
