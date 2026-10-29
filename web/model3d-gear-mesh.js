import * as THREE from 'three';

// Schematic teeth, counted correctly; never exported or represented as precision involute geometry.
function gearGeometry(part) {
  const pitch = part.module * part.teeth / 2 / 1000;
  const root = pitch - 1.25 * part.module / 1000, tip = pitch + part.module / 1000;
  const shape = new THREE.Shape(), step = Math.PI * 2 / part.teeth;
  for (let i = 0; i < part.teeth; i++) {
    for (const [fraction, radius] of [[-.5, root], [-.3, root], [-.2, tip], [.2, tip], [.3, root], [.5, root]]) {
      const a = (i + fraction) * step, x = Math.cos(a) * radius, y = Math.sin(a) * radius;
      if (i === 0 && fraction === -.5) shape.moveTo(x, y); else shape.lineTo(x, y);
    }
  }
  shape.closePath();
  const hole = new THREE.Path(); hole.absarc(0, 0, part.bore / 2000, 0, 2 * Math.PI, true);
  shape.holes.push(hole);
  const geometry = new THREE.ExtrudeGeometry(shape, { depth: part.face / 1000, bevelEnabled: false, curveSegments: 32 });
  geometry.translate(0, 0, -part.face / 2000);
  return geometry;
}

export function buildGearDisplay(snapshot) {
  const root = new THREE.Group();
  for (const p of snapshot.parts) {
    let geometry, color;
    if (p.id.startsWith('gear-')) { geometry = gearGeometry(p); color = p.id === 'gear-in' ? 0xefa855 : 0x48c5c7; }
    else if (p.id === 'frame') { geometry = new THREE.BoxGeometry(.08, .052, .004); color = 0x34465a; }
    else { geometry = new THREE.CylinderGeometry(.0025, .0025, .02, 32); geometry.rotateX(Math.PI / 2); color = 0xaebcca; }
    const mesh = new THREE.Mesh(geometry, new THREE.MeshStandardMaterial({ color, roughness: .45, metalness: .25 }));
    mesh.name = `part:${p.id}`; mesh.position.fromArray(p.position.map(v => v / 1000));
    mesh.userData.explode_dir = [0, 0, p.id === 'frame' ? -1 : p.id.startsWith('shaft') ? .3 : 1];
    mesh.userData.explode_distance = p.id.startsWith('shaft') ? .3 : 1;
    mesh.userData.label = ({ frame: 'Support frame', 'shaft-in': 'Input shaft', 'shaft-out': 'Output shaft',
      'gear-in': 'Input gear', 'gear-out': 'Output gear' })[p.id];
    mesh.userData.does = p.id.startsWith('gear') ? `${p.teeth} teeth · module ${p.module} mm · specified demo part`
      : 'Fixed support · hiding it changes only the view';
    root.add(mesh);
  }
  return root;
}

export function updateGearDisplay(root, snapshot) {
  for (const [index, key] of [[3, 'input'], [4, 'output']]) {
    const part = snapshot.parts[index], mesh = root.getObjectByName(`part:${part.id}`);
    const signature = `${part.teeth}:${part.module}:${part.bore}:${part.face}`;
    if (mesh.userData.signature !== signature) {
      mesh.geometry.dispose(); mesh.geometry = gearGeometry(part); mesh.userData.signature = signature;
    }
    mesh.rotation.z = snapshot.angles[key] * Math.PI / 180 + (key === 'output' ? Math.PI - Math.PI / part.teeth : 0);
  }
}
