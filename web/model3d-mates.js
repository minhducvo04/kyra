// Mates: joints that make an assembly move by rules. Pure functions, no DOM, no three.js.
// Mirror of src/companion/model3d_mates.py (schema and words documented there); tests/data/mates_vectors.json
// holds the shared vectors: poses agree within 1e-6, refusal strings exactly.
export const GEOMETRIC = new Set(['coincident','concentric','distance','angle']);
const KINDS = new Set(['fixed', 'revolute', 'slider', 'gear',...GEOMETRIC]);
const identity = () => [[1, 0, 0], [0, 1, 0], [0, 0, 1]];
const num = v => String(v);
const finite = v => typeof v === 'number' && Number.isFinite(v);
const vec3 = v => Array.isArray(v) && v.length === 3 && v.every(finite);
const hypot = v => Math.hypot(v[0], v[1], v[2]);
const isInt = v => Number.isInteger(v);
const partMap = assembly => Object.fromEntries((assembly.parts || []).map(p => [p.slug, p]));

function ratioOf(mate) {
  const teeth = mate.teeth;
  if (Array.isArray(teeth) && teeth.length === 2) return teeth[0] / teeth[1];
  return finite(mate.ratio) ? mate.ratio : null;
}

function axisOf(mate, parts) {
  if ('frame' in mate) {
    const parent = Array.isArray(mate.parts) && mate.parts.length ? parts[mate.parts[0]] : undefined;
    const frame = parent && parent.frames ? parent.frames[mate.frame] : undefined;
    if (!frame) return null;
    return [frame.axis, frame.origin.map((o, i) => parent.position[i] + o)];
  }
  if (mate.kind === 'slider') return vec3(mate.axis) ? [mate.axis, [0, 0, 0]] : null;
  return vec3(mate.axis) && vec3(mate.anchor) ? [mate.axis, mate.anchor] : null;
}

function checkFrames(parts) {
  const reasons = [];
  for (const part of parts) {
    for (const [name, frame] of Object.entries(part.frames || {})) {
      if (!frame || typeof frame !== 'object' || !vec3(frame.origin) || !vec3(frame.axis)) {
        reasons.push(`part '${part.slug}': frame '${name}' needs origin and axis`);
      } else if (hypot(frame.axis) < 1e-9) reasons.push(`part '${part.slug}': frame '${name}' axis must not be zero`);
    }
  }
  return reasons;
}

export function validate(assembly) {
  const parts = partMap(assembly);
  const reasons = checkFrames(assembly.parts || []);
  const mates = assembly.mates || [];
  if(!Array.isArray(mates) || mates.length>200 || mates.some(m=>!m || typeof m!=='object' || Array.isArray(m))) return ['mates must be a list of at most 200 objects'];
  const byId = {}, movedBy = {}, drives = {};
  for (const mate of mates) {
    const mid = mate.id;
    if (typeof mid !== 'string' || !mid) { reasons.push('mate without an id'); continue; }
    if (mid in byId) { reasons.push(`mate '${mid}': duplicate id`); continue; }
    byId[mid] = mate;
    const kind = mate.kind;
    if (!KINDS.has(kind)) { reasons.push(`mate '${mid}': unknown kind '${kind}'`); continue; }
    if (kind === 'gear') continue;
    const pair = mate.parts;
    if (!Array.isArray(pair) || pair.length !== 2 || pair[0] === pair[1]) {
      reasons.push(`mate '${mid}': parts must be two different part slugs`); continue;
    }
    const unknown = pair.filter(s => !(s in parts));
    if (unknown.length) { reasons.push(`mate '${mid}': unknown part '${unknown[0]}'`); continue; }
    if (pair[1] in movedBy && !(GEOMETRIC.has(kind) && GEOMETRIC.has(byId[movedBy[pair[1]]].kind))) { reasons.push(`part '${pair[1]}' is moved by mates '${movedBy[pair[1]]}' and '${mid}'`); continue; }
    movedBy[pair[1]] = mid;
    if(GEOMETRIC.has(kind)) {
      if('range' in mate) reasons.push(`mate '${mid}': geometric mates do not support range`);
      const refs=mate.refs, required=kind==='concentric' ? 'cylinder':'plane';
      if(!Array.isArray(refs) || refs.length!==2 || refs.some((r,i)=>!r || r.kind!==required || r.part!==pair[i] || !boundedVector(r.point_mm) || !boundedVector(r.direction) || hypot(r.direction)<1e-9)) reasons.push(`mate '${mid}': pick two ${required} references on its parts`);
      if(kind==='distance' && !bounded(mate.distance_mm)) reasons.push(`mate '${mid}': distance_mm must be finite`);
      if(kind==='angle' && !(finite(mate.angle_deg) && mate.angle_deg>=0 && mate.angle_deg<=180)) reasons.push(`mate '${mid}': angle_deg must be between 0 and 180`);
      if('opposed' in mate && typeof mate.opposed!=='boolean') reasons.push(`mate '${mid}': opposed must be boolean`);
      continue;
    }
    if (kind === 'fixed') continue;
    if ('frame' in mate && !((parts[pair[0]].frames || {})[mate.frame])) {
      reasons.push(`mate '${mid}': frame '${mate.frame}' not on part '${pair[0]}'`); continue;
    }
    const resolved = axisOf(mate, parts);
    if (resolved === null) {
      reasons.push(kind === 'revolute' ? `mate '${mid}': needs axis and anchor, or frame` : `mate '${mid}': needs axis or frame`);
      continue;
    }
    if (hypot(resolved[0]) < 1e-9) reasons.push(`mate '${mid}': axis must not be zero`);
    const rng = mate.range;
    if (rng !== undefined && rng !== null
      && !(Array.isArray(rng) && rng.length === 2 && rng.every(finite) && rng[0] < rng[1])) {
      reasons.push(`mate '${mid}': range must be [low, high] with low < high`);
    }
  }
  for (const mate of mates) {
    if (mate.kind !== 'gear' || !(mate.id in byId) || byId[mate.id] !== mate) continue;
    const mid = mate.id, linked = mate.mates;
    if (!Array.isArray(linked) || linked.length !== 2 || linked[0] === linked[1]) {
      reasons.push(`mate '${mid}': mates must be two different revolute ids`); continue;
    }
    const bad = linked.find(m => !(m in byId));
    if (bad !== undefined) { reasons.push(`mate '${mid}': unknown mate '${bad}'`); continue; }
    const wrong = linked.find(m => byId[m].kind !== 'revolute');
    if (wrong !== undefined) { reasons.push(`mate '${mid}': mate '${wrong}' is not revolute`); continue; }
    const teeth = mate.teeth;
    if (teeth !== undefined && teeth !== null
      && !(Array.isArray(teeth) && teeth.length === 2 && teeth.every(t => isInt(t) && t > 0))) {
      reasons.push(`mate '${mid}': teeth must be two positive whole numbers`); continue;
    }
    const ratio = ratioOf(mate);
    if (ratio === null) { reasons.push(`mate '${mid}': needs ratio or teeth`); continue; }
    if (ratio <= 0) { reasons.push(`mate '${mid}': ratio must be greater than 0`); continue; }
    if (teeth !== undefined && teeth !== null && finite(mate.ratio) && Math.abs(mate.ratio - ratio) > 1e-9) {
      reasons.push(`mate '${mid}': ratio ${num(mate.ratio)} does not match teeth ${teeth[0]}/${teeth[1]}`); continue;
    }
    for (const target of linked) {
      if (target in drives && drives[target] !== mid) {
        reasons.push(`mates '${drives[target]}' and '${mid}' both drive mate '${target}'`); break;
      }
      if (!(target in drives)) drives[target] = mid;
    }
  }
  if (!reasons.length) reasons.push(...loops(assembly.parts || [], byId));
  const driven = assembly.driven;
  if (driven !== undefined && driven !== null) {
    if (typeof driven !== 'object' || typeof driven.mate !== 'string' || !finite(driven.value)) {
      reasons.push('driven: needs a mate id and a finite value');
    } else if (!(driven.mate in byId)) reasons.push(`driven: unknown mate '${driven.mate}'`);
    else if (!['revolute', 'slider','concentric'].includes(byId[driven.mate].kind)) {
      reasons.push(`driven: mate '${driven.mate}' is not revolute or slider`);
    } else if (!reasons.length) reasons.push(...rangeReasons(byId, angles(byId, driven.mate, driven.value)));
  }
  return reasons;
}

function loops(parts, byId) {
  const parents=new Map(), visited=new Set(), active=new Set();
  for(const m of Object.values(byId)) if(m.kind!=='gear') {const group=parents.get(m.parts[1]) || [];group.push(m.parts[0]);parents.set(m.parts[1],group);}
  function visit(slug) {
    if(active.has(slug)) return true;if(visited.has(slug)) return false;
    active.add(slug);if((parents.get(slug)||[]).some(visit)) return true;
    active.delete(slug);visited.add(slug);return false;
  }
  for(const p of parts) if(visit(p.slug)) return [`mates form a loop through part '${p.slug}'`];
  return [];
}

function angles(byId, drivenId, value) {
  const values = { [drivenId]: value };
  let changed = true;
  while (changed) {
    changed = false;
    for (const mate of Object.values(byId)) {
      if (mate.kind !== 'gear') continue;
      const [a, b] = mate.mates, ratio = ratioOf(mate);
      if (a in values && !(b in values)) { values[b] = -values[a] * ratio; changed = true; }
      else if (b in values && !(a in values)) { values[a] = -values[b] / ratio; changed = true; }
    }
  }
  return values;
}

function rangeReasons(byId, values) {
  for (const [mid, mate] of Object.entries(byId)) {
    const rng = mate.range;
    if (rng && mid in values && !(rng[0] - 1e-9 <= values[mid] && values[mid] <= rng[1] + 1e-9)) {
      return [`mate '${mid}': value ${num(values[mid])} is outside range [${num(rng[0])}, ${num(rng[1])}]`];
    }
  }
  return [];
}

function rotation(axis, deg) {
  const length = hypot(axis), [x, y, z] = axis.map(a => a / length);
  const t = deg * Math.PI / 180, c = Math.cos(t), s = Math.sin(t), k = 1 - c;
  return [[c + x * x * k, x * y * k - z * s, x * z * k + y * s],
    [y * x * k + z * s, c + y * y * k, y * z * k - x * s],
    [z * x * k - y * s, z * y * k + x * s, c + z * z * k]];
}

const apply = (m, v) => m.map(row => row[0] * v[0] + row[1] * v[1] + row[2] * v[2]);

function compose(parent, own) {
  const rot = parent.rotation.map(row => [0, 1, 2].map(c => row[0] * own.rotation[0][c] + row[1] * own.rotation[1][c] + row[2] * own.rotation[2][c]));
  const moved = apply(parent.rotation, own.translation);
  return { rotation: rot, translation: moved.map((v, i) => v + parent.translation[i]) };
}

function ownPose(mate, parts, values) {
  if (mate.kind === 'fixed') return { rotation: identity(), translation: [0, 0, 0] };
  const [axis, anchor] = axisOf(mate, parts);
  const value = mate.id in values ? values[mate.id] : 0;
  if (mate.kind === 'slider') {
    const length = hypot(axis);
    return { rotation: identity(), translation: axis.map(a => a / length * value) };
  }
  const rot = rotation(axis, value), turned = apply(rot, anchor);
  return { rotation: rot, translation: anchor.map((a, i) => a - turned[i]) };
}

// Viewer readout only (no Python twin): the joint value of every mate the driven one reaches through gears.
export function jointValues(assembly, drivenValue = null) {
  const driven = assembly.driven;
  if (!driven) return {};
  const byId = Object.fromEntries((assembly.mates || []).map(m => [m.id, m]));
  return angles(byId, driven.mate, drivenValue === null ? driven.value : drivenValue);
}

export function solve(assembly, drivenValue = null) {
  const reasons = validate(assembly);
  if (reasons.length) return { ok: false, reason: reasons[0] };
  const parts = partMap(assembly);
  const byId = Object.fromEntries((assembly.mates || []).map(m => [m.id, m]));
  const driven = assembly.driven;
  let values = {};
  if ((drivenValue === null || drivenValue === undefined) && driven) drivenValue = driven.value;
  if (drivenValue !== null && drivenValue !== undefined) {
    if (!finite(drivenValue)) return { ok: false, reason: 'driven: value must be a finite number' };
    if (!driven) return { ok: false, reason: 'driven: the record names no driven mate' };
    values = angles(byId, driven.mate, drivenValue);
    const exceeded = rangeReasons(byId, values);
    if (exceeded.length) return { ok: false, reason: exceeded[0] };
  }
  const mateOf = {}, poses={}, freedom={};
  for(const m of Object.values(byId)) if(m.kind!=='gear') (mateOf[m.parts[1]] ||= []).push(m);
  const poseOf=slug => {
    if(slug in poses) return poses[slug];const group=mateOf[slug] || [];
    if(!group.length) poses[slug]=identityPose();
    else if(GEOMETRIC.has(group[0].kind)) {
      group.forEach(m=>poseOf(m.parts[0]));
      const result=geometricPose(group,poses,values);poses[slug]=result.pose;freedom[slug]=result.freedom;
    } else {const m=group[0];poses[slug]=compose(poseOf(m.parts[0]),ownPose(m,parts,values));}
    return poses[slug];
  };
  try {for(const slug of Object.keys(parts)) poseOf(slug);}
  catch(e) {return {ok:false,reason:e.message};}
  return {ok:true,poses,...(Object.keys(freedom).length ? {freedom}:{})};
}

export function applyEdit(assembly, edit) {
  const refused = { ok: false, assembly };
  if (!edit || typeof edit !== 'object' || typeof edit.mate !== 'string' || !edit.set || typeof edit.set !== 'object' || Array.isArray(edit.set)) {
    return { ...refused, reason: 'edit: needs mate and set' };
  }
  const mates = assembly.mates || [];
  const index = mates.findIndex(m => m.id === edit.mate);
  if (index < 0) return { ...refused, reason: `edit: unknown mate '${edit.mate}'` };
  if ('id' in edit.set || 'kind' in edit.set) return { ...refused, reason: 'edit: id and kind cannot change' };
  const candidate = structuredClone(assembly);
  Object.assign(candidate.mates[index], structuredClone(edit.set));
  if (JSON.stringify(candidate.mates[index]) === JSON.stringify(mates[index])) {
    return { ok: true, reason: 'This edit changes nothing.', assembly };
  }
  const reasons = validate(candidate);
  if (reasons.length) return { ...refused, reason: reasons[0] };
  return { ok: true, reason: 'Edit applied.', assembly: candidate };
}

// Six-column damped least squares, mirrored in model3d_mates.py.
const identityPose=()=>({rotation:identity(),translation:[0,0,0]});
const dot=(a,b)=>a.reduce((s,x,i)=>s+x*b[i],0);
const cross=(a,b)=>[a[1]*b[2]-a[2]*b[1],a[2]*b[0]-a[0]*b[2],a[0]*b[1]-a[1]*b[0]];
const unit=v=>v.map(x=>x/hypot(v));
function reference(ref,pose) {
  return [apply(pose.rotation,ref.point_mm.map(x=>x/1000)).map((x,i)=>x+pose.translation[i]),unit(apply(pose.rotation,ref.direction))];
}
function residual(group,parents,pose) {
  const out=[];
  for(const m of group) {
    const [a,n]=reference(m.refs[0],parents[m.parts[0]]),[b,v]=reference(m.refs[1],pose),delta=b.map((x,i)=>x-a[i]);
    if(m.kind==='angle' && ![0,180].includes(m.angle_deg)) out.push(10*(dot(n,v)-Math.cos(m.angle_deg*Math.PI/180)));
    else {
      const target=(m.kind==='angle' ? m.angle_deg===180:m.opposed) ? n.map(x=>-x):n;
      out.push(...v.map((x,i)=>10*(x-target[i])));
      if(m.kind==='angle') continue;
      if(m.kind==='concentric') {const along=dot(delta,n);out.push(...delta.map((x,i)=>1000*(x-along*n[i])));}
      else out.push(1000*dot(delta,n)-(m.distance_mm || 0));
    }
  }
  return out;
}
function increment(pose,step) {
  const angle=hypot(step.slice(0,3));
  return compose({rotation:angle>1e-15 ? rotation(step.slice(0,3),angle*180/Math.PI):identity(),translation:step.slice(3)},pose);
}
function jacobian(group,parents,pose,r) {
  const columns=[];
  for(let j=0;j<6;j++) {const step=Array(6).fill(0);step[j]=1e-6;columns.push(residual(group,parents,increment(pose,step)).map((x,i)=>(x-r[i])/1e-6));}
  return r.map((_,i)=>columns.map(c=>c[i]));
}
function linear(matrix,rhs) {
  const rows=matrix.map((r,i)=>[...r,rhs[i]]),n=rhs.length;
  for(let i=0;i<n;i++) {
    let pivot=i;for(let j=i+1;j<n;j++) if(Math.abs(rows[j][i])>Math.abs(rows[pivot][i])) pivot=j;
    [rows[i],rows[pivot]]=[rows[pivot],rows[i]];
    if(Math.abs(rows[i][i])<1e-16) return Array(n).fill(0);
    let scale=rows[i][i];rows[i]=rows[i].map(v=>v/scale);
    for(let j=0;j<n;j++) if(j!==i) {scale=rows[j][i];rows[j]=rows[j].map((x,k)=>x-scale*rows[i][k]);}
  }
  return rows.map(r=>r[n]);
}
function rank(rows) {
  const basis=[];
  for(const row of rows) {let v=[...row];for(const b of basis) {const projection=dot(v,b);v=v.map((x,i)=>x-projection*b[i]);}
    const length=Math.hypot(...v);if(length>1e-4) basis.push(v.map(x=>x/length));}
  return Math.min(6,basis.length);
}
function align(a,b) {
  let axis=cross(a,b);if(hypot(axis)<1e-9) {if(dot(a,b)>0) return identity();axis=cross(a,Math.abs(a[0])<.9 ? [1,0,0]:[0,1,0]);}
  return rotation(axis,Math.acos(Math.max(-1,Math.min(1,dot(a,b))))*180/Math.PI);
}
function geometricPose(group,parents,values) {
  let pose=identityPose();const first=group[0];let [,target]=reference(first.refs[0],parents[first.parts[0]]);
  if(first.opposed) target=target.map(x=>-x);
  if(first.kind==='angle') {const axis=cross(target,Math.abs(target[0])<.9 ? [1,0,0]:[0,1,0]);target=apply(rotation(axis,first.angle_deg),target);}
  pose.rotation=align(unit(first.refs[1].direction),target);
  for(let end=1;end<=group.length;end++) {
    const prefix=group.slice(0,end);
    for(let iteration=0;iteration<80;iteration++) {
      const r=residual(prefix,parents,pose);if(Math.max(0,...r.map(Math.abs))<1e-7) break;
      const jac=jacobian(prefix,parents,pose,r),normal=Array.from({length:6},(_,i)=>Array.from({length:6},(_,j)=>jac.reduce((s,row)=>s+row[i]*row[j],0)+(i===j ? 1e-8:0)));
      const step=linear(normal,Array.from({length:6},(_,i)=>-jac.reduce((s,row,k)=>s+row[i]*r[k],0))),old=dot(r,r);
      let improved=false;
      for(let power=0;power<16;power++) {const candidate=increment(pose,step.map(v=>v*2**(-power))),trial=residual(prefix,parents,candidate);
        if(dot(trial,trial)<old) {pose=candidate;improved=true;break;}}
      if(!improved) break;
    }
    if(Math.max(0,...residual(prefix,parents,pose).map(Math.abs))>1e-5) throw new Error(`mate '${group[end-1].id}': conflicts with earlier mates or cannot converge`);
  }
  // Match the backend's free-cylinder zero before applying the stored drive.
  for(const m of group) {
    if(m.kind!=='concentric') continue;
    let [,axis]=reference(m.refs[0],parents[m.parts[0]]);
    if(m.opposed) axis=axis.map(x=>-x);
    const ref=m.refs[1],[point]=reference(ref,pose),rot=align(unit(ref.direction),axis);
    const turned=apply(rot,ref.point_mm.map(x=>x/1000));
    const candidate={rotation:rot,translation:point.map((x,i)=>x-turned[i])};
    if(Math.max(0,...residual(group,parents,candidate).map(Math.abs))<1e-7) {pose=candidate;break;}
  }
  const r=residual(group,parents,pose),freedom=6-rank(jacobian(group,parents,pose,r));
  for(const m of group) if(m.id in values) {
    const [anchor,axis]=reference(m.refs[0],parents[m.parts[0]]),rot=rotation(axis,values[m.id]),turned=apply(rot,anchor);
    pose=compose({rotation:rot,translation:anchor.map((a,i)=>a-turned[i])},pose);
    if(Math.max(0,...residual(group,parents,pose).map(Math.abs))>1e-5) throw new Error(`mate '${m.id}': rotation is constrained by another mate`);
  }
  return {pose,freedom};
}

const bounded=v=>finite(v) && Math.abs(v)<=1e6;
const boundedVector=v=>Array.isArray(v) && v.length===3 && v.every(bounded);
