import * as THREE from 'three';

export function buildFlowDisplay(snapshot) {
  const root = new THREE.Group(), colors = [0xefa855, 0x48c5c7, 0x818ddd];
  for (const [i, s] of snapshot.segments.entries()) {
    const group = new THREE.Group(); group.name = `part:${s.id}`;
    group.userData.label = `${s.diameter * 1000} mm pipe`;
    group.userData.does = 'Specified section · constant volume flow, speed depends on area';
    group.userData.explode_dir = [0, 0, 0];
    const start = new THREE.Vector3(...s.start), end = new THREE.Vector3(...s.end);
    const mesh = new THREE.Mesh(new THREE.CylinderGeometry(s.diameter / 2, s.diameter / 2, s.length, 24, 1, true),
      new THREE.MeshStandardMaterial({ color: colors[i], transparent: true, opacity: .32,
        roughness: .7, side: THREE.DoubleSide, depthWrite: false }));
    mesh.position.copy(start).add(end).multiplyScalar(.5);
    mesh.quaternion.setFromUnitVectors(new THREE.Vector3(0, 1, 0), end.sub(start).normalize());
    group.add(mesh); root.add(group);
  }
  for (let i = 0; i < snapshot.particles.length; i++) {
    const dot = new THREE.Mesh(new THREE.SphereGeometry(.005, 8, 6),
      new THREE.MeshStandardMaterial({ color: 0xe8faff, emissive: 0x29b6cf, emissiveIntensity: .8 }));
    dot.name = `flow-marker-${i}`;
    root.children[snapshot.particles[i].segment].add(dot);
  }
  updateFlowDisplay(root, snapshot);
  return root;
}

export function updateFlowDisplay(root, snapshot) {
  snapshot.particles.forEach((p, i) => {
    const dot = root.getObjectByName(`flow-marker-${i}`);
    // Sections share a frame, so a marker stays continuous across section boundaries.
    const section = root.getObjectByName(`part:${snapshot.segments[p.segment].id}`);
    if (dot.parent !== section) section.add(dot);
    dot.position.fromArray(p.position);
  });
}
