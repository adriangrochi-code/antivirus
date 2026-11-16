//! Pruebas sobre un /sys falso (archivos normales). El planificador se prueba en la VM (tests/qemu-boot-test.sh).
use super::*;
use std::sync::Mutex;

static UNO_A_LA_VEZ: Mutex<()> = Mutex::new(());

fn sistema_falso(nombre: &str) -> PathBuf {
    let r = std::env::temp_dir().join(format!("tisma-ranuras-{nombre}-{}", std::process::id()));
    let _ = fs::remove_dir_all(&r);
    let f = |rel: &str, txt: &str| {
        let p = r.join(rel);
        fs::create_dir_all(p.parent().unwrap()).unwrap();
        fs::write(p, txt).unwrap();
    };
    for c in ["cpu0", "cpu1"] {
        f(
            &format!("sys/devices/system/cpu/{c}/cpufreq/scaling_available_governors"),
            "performance schedutil powersave\n",
        );
        f(
            &format!("sys/devices/system/cpu/{c}/cpufreq/scaling_governor"),
            "schedutil\n",
        );
    }
    f("sys/devices/system/cpu/cpuidle/available_governors", "menu teo\n");
    f("sys/devices/system/cpu/cpuidle/current_governor", "teo\n");
    f("sys/block/sda/queue/scheduler", "[mq-deadline] bfq none\n");
    f("sys/block/nvme0n1/queue/scheduler", "[none] mq-deadline\n");
    f("sys/block/loop0/queue/scheduler", "[none]\n");
    f("proc/sys/net/ipv4/tcp_available_congestion_control", "reno cubic bbr\n");
    f("proc/sys/net/ipv4/tcp_congestion_control", "cubic\n");
    f("sys/module/zswap/parameters/compressor", "zstd\n");
    f(
        "proc/crypto",
        "name         : zstd\ndriver       : zstd-generic\n\nname         : lz4\n",
    );
    f("sys/kernel/mm/lru_gen/enabled", "0x0007\n");
    f("sys/kernel/sched_ext/state", "disabled\n");
    f("sys/kernel/mm/transparent_hugepage/enabled", "always [madvise] never\n");
    f("sys/kernel/mm/transparent_hugepage/hpage_pmd_size", "2097152\n");
    for t in ["16kB", "64kB", "128kB", "2048kB"] {
        let m = if t == "2048kB" {
            "always [inherit] madvise never"
        } else {
            "always inherit madvise [never]"
        };
        f(&format!("sys/kernel/mm/transparent_hugepage/hugepages-{t}/enabled"), m);
    }
    f("usr/bin/scx_lavd", "");
    f("usr/bin/scx_../../etc/x", "");
    std::env::set_var("TISMA_RANURAS_RAIZ", &r);
    r
}

#[test]
fn opciones_salen_del_kernel() {
    let _g = UNO_A_LA_VEZ.lock().unwrap_or_else(|e| e.into_inner());
    sistema_falso("opciones");
    assert_eq!(Ranura::Red.opciones(), ["reno", "cubic", "bbr"]);
    assert_eq!(Ranura::Reposo.actual().as_deref(), Some("teo"));
    assert_eq!(
        Ranura::Disco.actual().as_deref(),
        Some("nvme0n1:none,sda:mq-deadline"),
        "sin loop"
    );
    assert_eq!(Ranura::Disco.opciones(), ["none", "mq-deadline", "bfq"]);
    assert_eq!(Ranura::Compresion.opciones(), ["lz4", "zstd"]);
    assert_eq!(Ranura::Memoria.actual().as_deref(), Some("mglru"));
    assert_eq!(Ranura::Planificador.opciones(), ["eevdf", "scx_lavd"]);
    assert_eq!(Ranura::Planificador.actual().as_deref(), Some("eevdf"));
}

#[test]
fn nombres_de_planificador_cerrados() {
    assert!(nombre_scx_valido("scx_lavd"));
    assert!(nombre_scx_valido("scx_tisma_2"));
    for malo in ["scx_", "lavd", "scx_../x", "scx_LAVD", "scx_a b", "/usr/bin/scx_lavd"] {
        assert!(!nombre_scx_valido(malo), "{malo}");
    }
}

#[test]
fn cambiar_y_revertir() {
    let _g = UNO_A_LA_VEZ.lock().unwrap_or_else(|e| e.into_inner());
    let r = sistema_falso("cambiar");
    cambiar(Ranura::Red, "bbr").unwrap();
    cambiar(Ranura::Frecuencia, "powersave").unwrap();
    cambiar(Ranura::Frecuencia, "performance").unwrap();
    assert_eq!(
        leer(&r.join("sys/devices/system/cpu/cpu1/cpufreq/scaling_governor")).as_deref(),
        Some("performance")
    );
    // se recuerda el ORIGINAL, no el intermedio
    assert_eq!(leer_anterior().get("frecuencia").map(String::as_str), Some("schedutil"));
    assert_eq!(revertir().unwrap(), 2);
    assert_eq!(Ranura::Red.actual().as_deref(), Some("cubic"));
    assert_eq!(Ranura::Frecuencia.actual().as_deref(), Some("schedutil"));
    assert!(leer_anterior().is_empty());
}

#[test]
fn rechaza_lo_que_el_kernel_no_ofrece() {
    let _g = UNO_A_LA_VEZ.lock().unwrap_or_else(|e| e.into_inner());
    let r = sistema_falso("rechaza");
    assert!(cambiar(Ranura::Red, "../../etc/shadow").is_err());
    assert!(cambiar(Ranura::Reposo, "haltpoll").is_err());
    assert!(cambiar(Ranura::Disco, "sda:kyber").is_err());
    assert!(cambiar(Ranura::Disco, "sdz:none").is_err(), "disco que no existe");
    assert_eq!(
        leer(&r.join("proc/sys/net/ipv4/tcp_congestion_control")).as_deref(),
        Some("cubic")
    );
}

#[test]
fn disco_solo_donde_hay_esa_opcion() {
    let _g = UNO_A_LA_VEZ.lock().unwrap_or_else(|e| e.into_inner());
    let r = sistema_falso("disco");
    // bfq existe en sda pero no en el NVMe: se aplica donde se puede
    cambiar(Ranura::Disco, "bfq").unwrap();
    assert_eq!(Ranura::Disco.actual().as_deref(), Some("nvme0n1:none,sda:bfq"));
    // el kernel real sigue mostrando todas las opciones tras escribir (un archivo normal no)
    fs::write(r.join("sys/block/sda/queue/scheduler"), "mq-deadline [bfq] none\n").unwrap();
    revertir().unwrap();
    assert!(Ranura::Disco.actual().unwrap().ends_with("sda:mq-deadline"));
}

#[test]
fn aplicar_es_todo_o_nada() {
    let _g = UNO_A_LA_VEZ.lock().unwrap_or_else(|e| e.into_inner());
    sistema_falso("aplicar");
    let e = aplicar("# perfil\nred = bbr\nmemoria=clasico\nreposo=haltpoll\n").unwrap_err();
    assert!(e.contains("haltpoll"), "{e}");
    assert_eq!(Ranura::Red.actual().as_deref(), Some("cubic"), "se deshizo red");
    assert_eq!(Ranura::Memoria.actual().as_deref(), Some("mglru"), "se deshizo memoria");
    assert!(aplicar("ranura_inventada=1").is_err());
    assert_eq!(aplicar("red=bbr\nreposo=menu\n").unwrap(), 2);
    assert_eq!(Ranura::Reposo.actual().as_deref(), Some("menu"));
}

#[test]
fn paginas_grandes_y_medianas() {
    let _g = UNO_A_LA_VEZ.lock().unwrap_or_else(|e| e.into_inner());
    let r = sistema_falso("paginas");
    assert_eq!(Ranura::Paginas.actual().as_deref(), Some("madvise"));
    assert_eq!(Ranura::Paginas.opciones(), ["always", "madvise", "never"]);
    // 2 MB no es «mediana»: la gobierna la ranura paginas
    assert_eq!(tamanos_mthp(), ["16kB", "64kB", "128kB"]);
    assert_eq!(Ranura::PaginasMedianas.actual().as_deref(), Some("ninguna"));
    assert!(Ranura::PaginasMedianas.opciones().contains(&"64kB:always".to_string()));
    assert!(!Ranura::PaginasMedianas.opciones().iter().any(|o| o.ends_with(":never")));

    // el orden de entrada no importa; se guarda en el orden del kernel
    cambiar(Ranura::PaginasMedianas, "128kB:inherit+64kB:always").unwrap();
    let leer_t = |t: &str| leer(&r.join(format!("sys/kernel/mm/transparent_hugepage/hugepages-{t}/enabled")));
    assert_eq!(leer_t("64kB").as_deref(), Some("always"));
    assert_eq!(leer_t("128kB").as_deref(), Some("inherit"));
    assert_eq!(leer_t("16kB").as_deref(), Some("never"));
    assert!(leer_t("2048kB").unwrap().contains("[inherit]"), "la de 2 MB no se toca");
    assert_eq!(
        Ranura::PaginasMedianas.actual().as_deref(),
        Some("64kB:always+128kB:inherit")
    );

    for malo in [
        "64kB:never",
        "64kB:always+64kB:inherit",
        "3kB:always",
        "64kB:always+ninguna",
        "../x:always",
    ] {
        assert!(cambiar(Ranura::PaginasMedianas, malo).is_err(), "{malo}");
    }
    cambiar(Ranura::Paginas, "never").unwrap();
    como_kernel(&r);
    revertir().unwrap();
    assert_eq!(Ranura::Paginas.actual().as_deref(), Some("madvise"));
    assert_eq!(Ranura::PaginasMedianas.actual().as_deref(), Some("ninguna"));
}

/// Un archivo normal guarda solo lo escrito («never»); el kernel sigue mostrando todas las opciones con la
/// elegida entre corchetes. Esto rehace ese formato en los archivos de THP del sistema falso.
fn como_kernel(r: &Path) {
    let base = r.join("sys/kernel/mm/transparent_hugepage");
    let mut archivos = vec![(base.join("enabled"), vec!["always", "madvise", "never"])];
    for t in ["16kB", "64kB", "128kB", "2048kB"] {
        archivos.push((
            base.join(format!("hugepages-{t}/enabled")),
            vec!["always", "inherit", "madvise", "never"],
        ));
    }
    for (p, opciones) in archivos {
        let v = fs::read_to_string(&p).unwrap().trim().to_string();
        if !v.contains('[') {
            let txt: Vec<String> = opciones
                .iter()
                .map(|o| if *o == v { format!("[{o}]") } else { o.to_string() })
                .collect();
            fs::write(&p, txt.join(" ")).unwrap();
        }
    }
}
