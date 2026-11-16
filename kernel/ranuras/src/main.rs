//! tisma-ranuras: capa de ranuras intercambiables del kernel TISMA.
//!
//! Linux ya permite cambiar muchas piezas en marcha (planificador con sched_ext, gobernadores, planificador de
//! disco, control de congestión…), pero cada una por una ruta distinta. Esta herramienta las presenta como
//! RANURAS con la misma forma:
//!   * las opciones salen del propio kernel (vocabulario cerrado: no se puede escribir nada que el kernel no
//!     ofrezca, ni rutas libres);
//!   * cada cambio se verifica leyendo de vuelta y, en el planificador, comprobando que sigue vivo;
//!   * si algo falla se vuelve al valor anterior; `aplicar` es todo o nada; `revertir` deshace lo de la sesión.
//!
//! Ninguna ranura toca mitigaciones, lockdown, firmas ni LSM: solo piezas de rendimiento y energía.
//!
//! Uso: tisma-ranuras listar | cambiar <ranura> <valor> | aplicar <archivo> | revertir

use std::collections::BTreeMap;
use std::fs;
use std::io::Write;
use std::os::unix::process::CommandExt;
use std::path::{Path, PathBuf};
use std::process::{Command, ExitCode, Stdio};
use std::thread::sleep;
use std::time::{Duration, Instant};

/// Raíz del sistema (solo cambia en las pruebas, para trabajar sobre un /sys falso).
fn raiz() -> PathBuf {
    PathBuf::from(std::env::var("TISMA_RANURAS_RAIZ").unwrap_or_else(|_| "/".into()))
}

fn ruta(rel: &str) -> PathBuf {
    raiz().join(rel.trim_start_matches('/'))
}

fn leer(p: &Path) -> Option<String> {
    fs::read_to_string(p).ok().map(|s| s.trim().to_string())
}

fn escribir(p: &Path, v: &str) -> Result<(), String> {
    fs::write(p, v).map_err(|e| format!("{}: {e}", p.display()))
}

/// Las opciones del kernel en formato «a [b] c» (el elegido entre corchetes) o «a b c».
fn palabras(s: &str) -> Vec<String> {
    s.split_whitespace()
        .map(|w| w.trim_matches(|c| c == '[' || c == ']').to_string())
        .collect()
}

fn entre_corchetes(s: &str) -> Option<String> {
    let p = palabras(s);
    match s.split_whitespace().find(|w| w.starts_with('[')) {
        Some(w) => Some(w.trim_matches(|c| c == '[' || c == ']').into()),
        None if p.len() == 1 => p.into_iter().next(), // una sola opción, sin corchetes
        None => None,
    }
}

/// Entradas de un directorio que cumplen un filtro, ordenadas (cpu0, cpu1… / sda, vda…).
fn entradas(dir: &str, filtro: impl Fn(&str) -> bool) -> Vec<String> {
    let mut v: Vec<String> = fs::read_dir(ruta(dir))
        .map(|it| {
            it.flatten()
                .filter_map(|e| e.file_name().into_string().ok())
                .filter(|n| filtro(n))
                .collect()
        })
        .unwrap_or_default();
    v.sort();
    v
}

// ------------------------------------------------------------------------------------------- ranuras
#[derive(Clone, Copy, Debug, PartialEq)]
enum Ranura {
    Planificador,
    Frecuencia,
    Reposo,
    Disco,
    Red,
    Compresion,
    Memoria,
}

const RANURAS: [Ranura; 7] = [
    Ranura::Planificador,
    Ranura::Frecuencia,
    Ranura::Reposo,
    Ranura::Disco,
    Ranura::Red,
    Ranura::Compresion,
    Ranura::Memoria,
];

const SCX_ESTADO: &str = "sys/kernel/sched_ext/state";
const SCX_OPS: &str = "sys/kernel/sched_ext/root/ops";
const SCX_DIRS: [&str; 2] = ["usr/bin", "usr/sbin"];
const ESTADO_DIR: &str = "run/tisma-ranuras";
const CPUIDLE: &str = "sys/devices/system/cpu/cpuidle";
const TCP: &str = "proc/sys/net/ipv4";
const ZSWAP: &str = "sys/module/zswap/parameters/compressor";
const LRU_GEN: &str = "sys/kernel/mm/lru_gen/enabled";
const COMPRESORES: [&str; 6] = ["lz4", "lz4hc", "lzo", "lzo-rle", "zstd", "deflate"];

impl Ranura {
    fn nombre(self) -> &'static str {
        match self {
            Ranura::Planificador => "planificador",
            Ranura::Frecuencia => "frecuencia",
            Ranura::Reposo => "reposo",
            Ranura::Disco => "disco",
            Ranura::Red => "red",
            Ranura::Compresion => "compresion",
            Ranura::Memoria => "memoria",
        }
    }

    fn descripcion(self) -> &'static str {
        match self {
            Ranura::Planificador => "quién decide qué programa usa la CPU (eevdf del kernel o sched_ext)",
            Ranura::Frecuencia => "gobernador de frecuencia de la CPU",
            Ranura::Reposo => "gobernador de reposo de la CPU (cpuidle)",
            Ranura::Disco => "planificador de E/S de los discos",
            Ranura::Red => "control de congestión de TCP",
            Ranura::Compresion => "compresor de la memoria comprimida (zswap)",
            Ranura::Memoria => "algoritmo para elegir qué memoria liberar (MGLRU o clásico)",
        }
    }

    fn por_nombre(n: &str) -> Option<Ranura> {
        RANURAS.into_iter().find(|r| r.nombre() == n)
    }

    /// Opciones que ofrece ESTE kernel en ESTE equipo. Vacío = ranura no disponible.
    fn opciones(self) -> Vec<String> {
        match self {
            Ranura::Planificador => {
                if !ruta(SCX_ESTADO).exists() {
                    return vec![];
                }
                let mut v = vec!["eevdf".to_string()];
                for d in SCX_DIRS {
                    for n in entradas(d, nombre_scx_valido) {
                        if !v.contains(&n) {
                            v.push(n);
                        }
                    }
                }
                v
            }
            Ranura::Frecuencia => cpus()
                .first()
                .and_then(|c| leer(&ruta(&format!("{c}/cpufreq/scaling_available_governors"))))
                .map(|s| palabras(&s))
                .unwrap_or_default(),
            Ranura::Reposo => leer(&ruta(&format!("{CPUIDLE}/available_governors")))
                .map(|s| palabras(&s))
                .unwrap_or_default(),
            Ranura::Disco => {
                let mut v: Vec<String> = vec![];
                for d in discos() {
                    for o in leer(&ruta(&format!("sys/block/{d}/queue/scheduler")))
                        .map(|s| palabras(&s))
                        .unwrap_or_default()
                    {
                        if !v.contains(&o) {
                            v.push(o);
                        }
                    }
                }
                v
            }
            Ranura::Red => leer(&ruta(&format!("{TCP}/tcp_available_congestion_control")))
                .map(|s| palabras(&s))
                .unwrap_or_default(),
            Ranura::Compresion => {
                if !ruta(ZSWAP).exists() {
                    return vec![];
                }
                let crypto = leer(&ruta("proc/crypto")).unwrap_or_default();
                let disponibles: Vec<&str> = crypto
                    .lines()
                    .filter_map(|l| l.strip_prefix("name"))
                    .map(|l| l.trim_start_matches([' ', '\t', ':']).trim())
                    .collect();
                COMPRESORES
                    .iter()
                    .filter(|c| disponibles.contains(c))
                    .map(|c| c.to_string())
                    .collect()
            }
            Ranura::Memoria => {
                if ruta(LRU_GEN).exists() {
                    vec!["mglru".into(), "clasico".into()]
                } else {
                    vec![]
                }
            }
        }
    }

    fn actual(self) -> Option<String> {
        match self {
            Ranura::Planificador => {
                if leer(&ruta(SCX_ESTADO))?.as_str() != "enabled" {
                    return Some("eevdf".into());
                }
                // ops = «lavd_1.1.4_x86_64…» → scx_lavd
                let ops = leer(&ruta(SCX_OPS)).unwrap_or_default();
                Some(format!("scx_{}", ops.split('_').next().unwrap_or("desconocido")))
            }
            Ranura::Frecuencia => cpus()
                .first()
                .and_then(|c| leer(&ruta(&format!("{c}/cpufreq/scaling_governor")))),
            Ranura::Reposo => leer(&ruta(&format!("{CPUIDLE}/current_governor")))
                .or_else(|| leer(&ruta(&format!("{CPUIDLE}/current_governor_ro")))),
            Ranura::Disco => {
                let v: Vec<String> = discos()
                    .into_iter()
                    .filter_map(|d| {
                        leer(&ruta(&format!("sys/block/{d}/queue/scheduler")))
                            .and_then(|s| entre_corchetes(&s))
                            .map(|s| format!("{d}:{s}"))
                    })
                    .collect();
                (!v.is_empty()).then(|| v.join(","))
            }
            Ranura::Red => leer(&ruta(&format!("{TCP}/tcp_congestion_control"))),
            Ranura::Compresion => leer(&ruta(ZSWAP)),
            Ranura::Memoria => leer(&ruta(LRU_GEN)).map(|v| {
                // el kernel muestra una máscara (0x0007); acepta también y/n
                let n = match v.as_str() {
                    "y" => 1,
                    "n" => 0,
                    _ => u64::from_str_radix(v.trim_start_matches("0x"), 16).unwrap_or(0),
                };
                if n != 0 {
                    "mglru".into()
                } else {
                    "clasico".into()
                }
            }),
        }
    }

    /// Valida contra las opciones del kernel, escribe y comprueba leyendo de vuelta.
    fn cambiar(self, valor: &str) -> Result<(), String> {
        let ops = self.opciones();
        if ops.is_empty() {
            return Err(format!(
                "la ranura «{}» no está disponible en este kernel",
                self.nombre()
            ));
        }
        // disco acepta además «dispositivo:valor,…» (así se guarda el estado anterior por disco)
        let por_disco = self == Ranura::Disco && valor.contains(':');
        if !por_disco && !ops.iter().any(|o| o == valor) {
            return Err(format!(
                "«{valor}» no es una opción de «{}» (opciones: {})",
                self.nombre(),
                ops.join(" ")
            ));
        }
        match self {
            Ranura::Planificador => cambiar_planificador(valor)?,
            Ranura::Frecuencia => {
                for c in cpus() {
                    let p = ruta(&format!("{c}/cpufreq/scaling_governor"));
                    if p.exists() {
                        escribir(&p, valor)?;
                    }
                }
            }
            Ranura::Reposo => escribir(&ruta(&format!("{CPUIDLE}/current_governor")), valor)?,
            Ranura::Disco => {
                let pares: Vec<(String, String)> = if por_disco {
                    let mut v = vec![];
                    for par in valor.split(',') {
                        let (d, s) = par.split_once(':').ok_or_else(|| format!("par no válido «{par}»"))?;
                        if !discos().iter().any(|x| x == d) {
                            return Err(format!("disco desconocido «{d}»"));
                        }
                        v.push((d.to_string(), s.to_string()));
                    }
                    v
                } else {
                    discos().into_iter().map(|d| (d, valor.to_string())).collect()
                };
                for (d, s) in pares {
                    let p = ruta(&format!("sys/block/{d}/queue/scheduler"));
                    // cada disco solo acepta sus propias opciones (p. ej. un NVMe sin bfq)
                    let suyas = leer(&p).map(|x| palabras(&x)).unwrap_or_default();
                    if suyas.contains(&s) {
                        escribir(&p, &s)?;
                    } else if por_disco {
                        return Err(format!("«{s}» no es una opción del disco {d}"));
                    }
                }
            }
            Ranura::Red => escribir(&ruta(&format!("{TCP}/tcp_congestion_control")), valor)?,
            Ranura::Compresion => escribir(&ruta(ZSWAP), valor)?,
            Ranura::Memoria => escribir(&ruta(LRU_GEN), if valor == "mglru" { "y" } else { "n" })?,
        }
        // Verificación: el kernel puede aceptar la escritura y no aplicarla
        let ahora = self.actual().unwrap_or_default();
        let ok = match self {
            Ranura::Disco if !por_disco => ahora.split(',').all(|p| {
                let (d, s) = p.split_once(':').unwrap_or(("", ""));
                s == valor
                    || !leer(&ruta(&format!("sys/block/{d}/queue/scheduler")))
                        .map(|x| palabras(&x))
                        .unwrap_or_default()
                        .contains(&valor.to_string())
            }),
            _ => ahora == valor,
        };
        if ok {
            Ok(())
        } else {
            Err(format!(
                "el kernel no aplicó «{valor}» en «{}» (sigue «{ahora}»)",
                self.nombre()
            ))
        }
    }
}

fn cpus() -> Vec<String> {
    entradas("sys/devices/system/cpu", |n| {
        n.strip_prefix("cpu")
            .is_some_and(|r| !r.is_empty() && r.bytes().all(|b| b.is_ascii_digit()))
    })
    .into_iter()
    .map(|c| format!("sys/devices/system/cpu/{c}"))
    .collect()
}

/// Discos reales con planificador (sin loop, zram ni ram: no tiene sentido cambiarles el planificador).
fn discos() -> Vec<String> {
    entradas("sys/block", |n| {
        !["loop", "zram", "ram", "dm-", "md"].iter().any(|p| n.starts_with(p))
    })
    .into_iter()
    .filter(|d| ruta(&format!("sys/block/{d}/queue/scheduler")).exists())
    .collect()
}

// ------------------------------------------------------------------------------------- planificador
/// Solo programas `scx_<letras/números/_>` de /usr/bin o /usr/sbin: nada de rutas libres.
fn nombre_scx_valido(n: &str) -> bool {
    n.strip_prefix("scx_").is_some_and(|r| {
        !r.is_empty()
            && r.bytes()
                .all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || b == b'_')
    })
}

fn espera_scx() -> Duration {
    Duration::from_secs(
        std::env::var("TISMA_RANURAS_ESPERA")
            .ok()
            .and_then(|s| s.parse().ok())
            .unwrap_or(60),
    )
}

fn pid_planificador() -> Option<(u32, String)> {
    let s = leer(&ruta(&format!("{ESTADO_DIR}/planificador.pid")))?;
    let (pid, nombre) = s.split_once(' ')?;
    Some((pid.parse().ok()?, nombre.to_string()))
}

/// El proceso sigue vivo y es el planificador que lanzamos (no un PID reutilizado por otro programa).
fn vivo(pid: u32, nombre: &str) -> bool {
    leer(&ruta(&format!("proc/{pid}/comm"))).is_some_and(|c| nombre.starts_with(&c) || c.starts_with(nombre))
}

fn esperar(cond: impl Fn() -> bool, max: Duration) -> bool {
    let t = Instant::now();
    while t.elapsed() < max {
        if cond() {
            return true;
        }
        sleep(Duration::from_millis(250));
    }
    cond()
}

fn detener_planificador() -> Result<(), String> {
    if let Some((pid, nombre)) = pid_planificador() {
        if vivo(pid, &nombre) {
            let _ = Command::new("kill").arg(pid.to_string()).status();
        }
        let _ = fs::remove_file(ruta(&format!("{ESTADO_DIR}/planificador.pid")));
    }
    // sched_ext vuelve solo a eevdf al cerrarse el programa
    if esperar(|| leer(&ruta(SCX_ESTADO)).as_deref() != Some("enabled"), espera_scx()) {
        Ok(())
    } else {
        Err("sched_ext sigue activo: lo controla otro programa (¿un servicio scx?)".into())
    }
}

fn cambiar_planificador(valor: &str) -> Result<(), String> {
    detener_planificador()?;
    if valor == "eevdf" {
        return Ok(());
    }
    let bin = SCX_DIRS
        .iter()
        .map(|d| ruta(d).join(valor))
        .find(|p| p.is_file())
        .ok_or_else(|| format!("no está instalado {valor}"))?;
    fs::create_dir_all(ruta(ESTADO_DIR)).map_err(|e| e.to_string())?;
    let log = fs::File::create(ruta(&format!("{ESTADO_DIR}/{valor}.log"))).map_err(|e| e.to_string())?;
    let mut hijo = Command::new(&bin)
        .stdin(Stdio::null())
        .stdout(log.try_clone().map_err(|e| e.to_string())?)
        .stderr(log)
        .process_group(0) // sobrevive a esta herramienta y a la terminal
        .spawn()
        .map_err(|e| format!("{}: {e}", bin.display()))?;
    let pid = hijo.id();
    escribir(
        &ruta(&format!("{ESTADO_DIR}/planificador.pid")),
        &format!("{pid} {valor}"),
    )?;
    let esperado = valor.trim_start_matches("scx_").to_string();
    let activo = || {
        leer(&ruta(SCX_ESTADO)).as_deref() == Some("enabled")
            && leer(&ruta(SCX_OPS)).is_some_and(|o| o.split('_').next() == Some(esperado.as_str()))
    };
    // Salud: que tome el control y SIGA activo un momento después (sched_ext lo expulsa si se porta mal).
    // Si el programa termina antes (error al cargar), no se espera más.
    let t = Instant::now();
    let mut termino = false;
    while t.elapsed() < espera_scx() && !activo() {
        if matches!(hijo.try_wait(), Ok(Some(_))) {
            termino = true;
            break;
        }
        sleep(Duration::from_millis(250));
    }
    if !termino && activo() {
        sleep(Duration::from_secs(2));
        if activo() && matches!(hijo.try_wait(), Ok(None)) {
            return Ok(());
        }
    }
    let _ = detener_planificador();
    Err(format!(
        "{valor} no tomó el control o se cayó (registro en /{ESTADO_DIR}/{valor}.log)"
    ))
}

// -------------------------------------------------------------------------- estado anterior y órdenes
fn archivo_anterior() -> PathBuf {
    ruta(&format!("{ESTADO_DIR}/anterior"))
}

fn leer_anterior() -> BTreeMap<String, String> {
    leer(&archivo_anterior())
        .unwrap_or_default()
        .lines()
        .filter_map(|l| l.split_once('=').map(|(a, b)| (a.to_string(), b.to_string())))
        .collect()
}

/// Guarda el valor ORIGINAL de cada ranura (el de antes del primer cambio), para `revertir`.
fn recordar(r: Ranura, valor: &str) -> Result<(), String> {
    let mut m = leer_anterior();
    if m.contains_key(r.nombre()) {
        return Ok(());
    }
    m.insert(r.nombre().into(), valor.into());
    fs::create_dir_all(ruta(ESTADO_DIR)).map_err(|e| e.to_string())?;
    let mut f = fs::File::create(archivo_anterior()).map_err(|e| e.to_string())?;
    for (k, v) in m {
        writeln!(f, "{k}={v}").map_err(|e| e.to_string())?;
    }
    Ok(())
}

/// Cambia una ranura; si falla, la deja como estaba.
fn cambiar(r: Ranura, valor: &str) -> Result<(), String> {
    let antes = r
        .actual()
        .ok_or_else(|| format!("la ranura «{}» no está disponible", r.nombre()))?;
    if antes == valor {
        return Ok(());
    }
    if let Err(e) = r.cambiar(valor) {
        let _ = r.cambiar(&antes);
        return Err(e);
    }
    recordar(r, &antes)
}

/// Archivo de líneas `ranura=valor` (# comentarios). Todo o nada: si una falla, se deshacen las anteriores.
fn aplicar(texto: &str) -> Result<usize, String> {
    let mut plan = vec![];
    for (i, l) in texto.lines().enumerate() {
        let l = l.split('#').next().unwrap_or("").trim();
        if l.is_empty() {
            continue;
        }
        let (k, v) = l
            .split_once('=')
            .ok_or_else(|| format!("línea {}: se espera ranura=valor", i + 1))?;
        let r = Ranura::por_nombre(k.trim())
            .ok_or_else(|| format!("línea {}: ranura desconocida «{}»", i + 1, k.trim()))?;
        plan.push((r, v.trim().to_string()));
    }
    let mut hechos: Vec<(Ranura, String)> = vec![];
    for (r, v) in &plan {
        let antes = r.actual().unwrap_or_default();
        if let Err(e) = cambiar(*r, v) {
            for (hr, hv) in hechos.iter().rev() {
                let _ = hr.cambiar(hv);
            }
            return Err(format!("{e}; se deshicieron los cambios anteriores"));
        }
        hechos.push((*r, antes));
    }
    Ok(plan.len())
}

fn revertir() -> Result<usize, String> {
    let m = leer_anterior();
    let mut errores = vec![];
    // el planificador al final: es el único cambio que tarda
    let mut orden: Vec<_> = m.iter().collect();
    orden.sort_by_key(|(k, _)| k.as_str() == "planificador");
    for (k, v) in &orden {
        if let Some(r) = Ranura::por_nombre(k) {
            if let Err(e) = r.cambiar(v) {
                errores.push(e);
            }
        }
    }
    if errores.is_empty() {
        let _ = fs::remove_file(archivo_anterior());
        Ok(orden.len())
    } else {
        Err(errores.join("; "))
    }
}

fn listar() {
    for r in RANURAS {
        let ops = r.opciones();
        if ops.is_empty() {
            println!("{}\t-\t-\t{} (no disponible)", r.nombre(), r.descripcion());
        } else {
            println!(
                "{}\t{}\t{}\t{}",
                r.nombre(),
                r.actual().unwrap_or_else(|| "?".into()),
                ops.join(","),
                r.descripcion()
            );
        }
    }
}

fn main() -> ExitCode {
    let args: Vec<String> = std::env::args().skip(1).collect();
    let res: Result<(), String> = match args.iter().map(String::as_str).collect::<Vec<_>>().as_slice() {
        ["listar"] | [] => {
            listar();
            Ok(())
        }
        ["cambiar", r, v] => match Ranura::por_nombre(r) {
            Some(r) => cambiar(r, v).map(|_| println!("{}={v}", r.nombre())),
            None => Err(format!("ranura desconocida «{r}»")),
        },
        ["aplicar", archivo] => fs::read_to_string(archivo)
            .map_err(|e| format!("{archivo}: {e}"))
            .and_then(|t| aplicar(&t))
            .map(|n| println!("aplicadas {n} ranuras")),
        ["revertir"] => revertir().map(|n| println!("revertidas {n} ranuras")),
        _ => Err("uso: tisma-ranuras listar | cambiar <ranura> <valor> | aplicar <archivo> | revertir".into()),
    };
    match res {
        Ok(()) => ExitCode::SUCCESS,
        Err(e) => {
            eprintln!("tisma-ranuras: {e}");
            ExitCode::FAILURE
        }
    }
}

#[cfg(test)]
mod pruebas;
