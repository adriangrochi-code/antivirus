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
