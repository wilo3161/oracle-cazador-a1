"""
Cazador Persistente de Capacidad Always Free en OCI para M.A.I.A. (Edición 2026)
Location: scripts/a1_capacity_hunter.py

Sondea en sa-bogota-1 la disponibilidad de máquinas Always Free ($0.00 perpetuo) en este orden:
  1. VM.Standard.A1.Flex 2 OCPU / 12 GB  (máximo permitido por el límite A1 de esta cuenta)
  2. VM.Standard.A1.Flex 1 OCPU / 6 GB   (más fácil de conseguir; ampliable luego a 2/12 sin costo)
  3. VM.Standard.E2.1.Micro              (respaldo: 1/8 OCPU, 1 GB; sirve como relé/túnel)

Garantías de costo cero (fail-closed):
  - Solo se lanzan formas de ALWAYS_FREE_SHAPES; cualquier otra forma se rechaza antes de llamar a OCI.
  - Antes de lanzar se consulta el límite de servicio: si no hay cupo gratuito, no se pide la máquina.
  - El almacenamiento de bloques total (boot + block volumes) nunca supera los 200 GB Always Free.
  - Las cuentas Free Tier no pueden facturar; aun así el cazador nunca usa formas de pago.

Al capturar A1 la misión termina. Si se captura la E2.1.Micro se avisa y se sigue cazando A1.
Uso: python scripts/a1_capacity_hunter.py [--dry-run] [--once]
"""

import os
import sys
import time
import uuid
import random
import base64
import logging
import argparse

import requests
from dotenv import load_dotenv

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
# La tarea programada no hereda el entorno de MAIA: sin esto el aviso de Telegram nunca se enviaba
load_dotenv(os.path.join(PROJECT_ROOT, ".env"))

LOG_DIR = os.path.join(PROJECT_ROOT, "data")
os.makedirs(LOG_DIR, exist_ok=True)
LOG_FILE = os.path.join(LOG_DIR, "oracle_cloud_hunter.log")
PID_FILE = os.path.join(LOG_DIR, "oracle_hunter.pid")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger("OracleA1Hunter")

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# ── Política Always Free ─────────────────────────────────────────────────────
ALWAYS_FREE_SHAPES = {"VM.Standard.A1.Flex", "VM.Standard.E2.1.Micro"}
ALWAYS_FREE_BLOCK_STORAGE_GB = 200
A1_NAME = "maia-a1-alwaysfree"
MICRO_NAME = "maia-micro-alwaysfree"

# Objetivos en orden de preferencia
TARGETS = [
    {"key": "a1_2", "name": A1_NAME, "shape": "VM.Standard.A1.Flex", "ocpus": 2.0, "ram": 12.0, "boot_gb": 100,
     "limits": {"standard-a1-core-regional-count": 2, "standard-a1-memory-regional-count": 12}},
    {"key": "a1_1", "name": A1_NAME, "shape": "VM.Standard.A1.Flex", "ocpus": 1.0, "ram": 6.0, "boot_gb": 100,
     "limits": {"standard-a1-core-regional-count": 1, "standard-a1-memory-regional-count": 6}},
    {"key": "micro", "name": MICRO_NAME, "shape": "VM.Standard.E2.1.Micro", "ocpus": None, "ram": None, "boot_gb": 50,
     "limits": {"vm-standard-e2-1-micro-count": 1}},
]

# Ritmo de sondeo: más espaciado que antes para reducir los 429 de OCI
BASE_SLEEP = (90.0, 150.0)
MAX_BACKOFF = 600.0


def notify_telegram(message: str):
    """Envía una notificación al Jefe por Telegram."""
    if not BOT_TOKEN or not CHAT_ID:
        logger.warning("Telegram bot token o chat ID no configurado.")
        return
    try:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
        res = requests.post(url, json={"chat_id": CHAT_ID, "text": message, "parse_mode": "Markdown"}, timeout=12)
        if res.status_code == 200:
            logger.info("✅ Notificación de Telegram enviada exitosamente al Jefe.")
        else:
            logger.warning(f"Telegram returned status {res.status_code}: {res.text[:200]}")
    except Exception as e:
        logger.error(f"Failed to send Telegram notification: {e}")


def acquire_lock() -> bool:
    try:
        import psutil
        my_pid = os.getpid()
        if os.path.exists(PID_FILE):
            try:
                with open(PID_FILE, "r", encoding="utf-8") as f:
                    content = f.read().strip()
                    old_pid = int(content) if content.isdigit() else None
                if old_pid and old_pid != my_pid and psutil.pid_exists(old_pid):
                    proc = psutil.Process(old_pid)
                    if proc.is_running() and "python" in proc.name().lower():
                        logger.info(f"Hunter ya se encuentra en ejecución (PID {old_pid}). Saliendo...")
                        return False
            except Exception:
                pass
        with open(PID_FILE, "w", encoding="utf-8") as f:
            f.write(str(my_pid))
        return True
    except Exception:
        return True


def get_ubuntu_image(compute_client, compartment_id, shape: str, arch_hint: str):
    """Última imagen oficial de Ubuntu 24.04 compatible con la forma (aarch64 para A1, x86_64 para Micro)."""
    try:
        images = compute_client.list_images(
            compartment_id,
            operating_system="Canonical Ubuntu",
            operating_system_version="24.04",
            shape=shape,
            sort_by="TIMECREATED",
            sort_order="DESC"
        ).data
        images = [img for img in images if "minimal" not in img.display_name.lower()] or images
        if arch_hint == "aarch64":
            images = [img for img in images if "aarch64" in img.display_name.lower()]
        else:
            images = [img for img in images if "aarch64" not in img.display_name.lower()]
        if images:
            return images[0].id
    except Exception as e:
        logger.error(f"Error buscando imagen Ubuntu para {shape}: {e}")
    return None


def create_resilient_clients(config):
    """Clientes OCI con reintentos y full jitter."""
    import oci
    retry_strategy = oci.retry.RetryStrategyBuilder(
        max_attempts_check=True,
        max_attempts=3,
        total_elapsed_time_check=True,
        total_elapsed_time_seconds=90,
        retry_max_wait_between_calls_seconds=25,
        retry_base_sleep_time_seconds=3,
        service_error_check=True,
        service_error_retry_on_any_5xx=True,
        service_error_retry_config={429: []},
        backoff_type=oci.retry.BACKOFF_FULL_JITTER_EQUAL_ON_THROTTLE_VALUE
    ).get_retry_strategy()
    return (
        oci.core.ComputeClient(config, retry_strategy=retry_strategy),
        oci.identity.IdentityClient(config, retry_strategy=retry_strategy),
        oci.core.VirtualNetworkClient(config, retry_strategy=retry_strategy),
        oci.core.BlockstorageClient(config, retry_strategy=retry_strategy),
        oci.limits.LimitsClient(config, retry_strategy=retry_strategy),
    )


def free_limit_available(limits_client, tenancy_id, ad_name, required: dict) -> bool:
    """Comprueba que el límite de servicio gratuito tenga cupo para la forma (evita LimitExceeded y 429)."""
    for name, needed in required.items():
        try:
            try:
                ra = limits_client.get_resource_availability("compute", name, tenancy_id).data
            except Exception:
                ra = limits_client.get_resource_availability("compute", name, tenancy_id, availability_domain=ad_name).data
            if (ra.available or 0) < needed:
                logger.info(f"Límite {name}: disponible={ra.available} < requerido={needed}. Se omite este objetivo.")
                return False
        except Exception as e:
            logger.warning(f"No se pudo leer el límite {name} ({e}); se omite el objetivo por seguridad.")
            return False
    return True


def used_block_storage_gb(block_client, tenancy_id, ad_name) -> int:
    """GB de almacenamiento de bloques en uso (boot + block volumes no terminados)."""
    import oci
    total = 0
    for bv in oci.pagination.list_call_get_all_results(block_client.list_boot_volumes, availability_domain=ad_name, compartment_id=tenancy_id).data:
        if bv.lifecycle_state not in ("TERMINATED", "TERMINATING"):
            total += bv.size_in_gbs or 0
    for v in oci.pagination.list_call_get_all_results(block_client.list_volumes, compartment_id=tenancy_id).data:
        if v.lifecycle_state not in ("TERMINATED", "TERMINATING"):
            total += v.size_in_gbs or 0
    return int(total)


def find_active_instance(compute_client, tenancy_id, name):
    existing = compute_client.list_instances(compartment_id=tenancy_id, display_name=name).data
    active = [i for i in existing if i.lifecycle_state not in ("TERMINATED", "TERMINATING")]
    return active[0] if active else None


def instance_public_ip(compute_client, net_client, tenancy_id, inst):
    vnics = compute_client.list_vnic_attachments(compartment_id=tenancy_id, instance_id=inst.id).data
    if vnics:
        return net_client.get_vnic(vnics[0].vnic_id).data.public_ip or "Pendiente"
    return "Pendiente"


def build_cloud_init(hostname: str, full_stack: bool) -> str:
    packages = ["curl", "git", "ufw", "fail2ban", "htop"]
    if full_stack:
        packages += ["python3.12", "python3.12-venv", "python3-pip", "redis-server"]
    run = [
        "curl -fsSL https://tailscale.com/install.sh | sh",
        "systemctl enable --now tailscaled",
        "mkdir -p /opt/maia/data /opt/maia/logs",
        "chown -R ubuntu:ubuntu /opt/maia",
        f"echo 'MAIA Always Free {hostname} lista' > /opt/maia/status.txt",
    ]
    if full_stack:
        run[2:2] = ["systemctl enable redis-server", "systemctl start redis-server"]
    lines = ["#cloud-config", f"hostname: {hostname}", "timezone: America/Guayaquil", "package_update: true", "packages:"]
    lines += [f"  - {p}" for p in packages]
    lines += ["runcmd:"] + [f"  - {c}" for c in run]
    return "\n".join(lines) + "\n"


def build_launch_details(target, tenancy_id, ad_name, subnet_id, image_id, ssh_pub_key):
    import oci
    if target["shape"] not in ALWAYS_FREE_SHAPES:
        raise RuntimeError(f"Forma {target['shape']} no es Always Free: lanzamiento bloqueado.")
    is_a1 = target["shape"] == "VM.Standard.A1.Flex"
    hostname = "maia-a1-alwaysfree" if is_a1 else "maia-micro-alwaysfree"
    user_data = base64.b64encode(build_cloud_init(hostname, full_stack=is_a1).encode("utf-8")).decode("utf-8")
    shape_config = None
    if is_a1:
        shape_config = oci.core.models.LaunchInstanceShapeConfigDetails(ocpus=target["ocpus"], memory_in_gbs=target["ram"])
    return oci.core.models.LaunchInstanceDetails(
        compartment_id=tenancy_id,
        availability_domain=ad_name,
        display_name=target["name"],
        shape=target["shape"],
        shape_config=shape_config,
        source_details=oci.core.models.InstanceSourceViaImageDetails(image_id=image_id, boot_volume_size_in_gbs=target["boot_gb"]),
        create_vnic_details=oci.core.models.CreateVnicDetails(
            subnet_id=subnet_id,
            display_name=f"{target['name']}-vnic",
            assign_public_ip=True,
            hostname_label="maiaa1" if is_a1 else "maiamicro"
        ),
        metadata={"ssh_authorized_keys": ssh_pub_key, "user_data": user_data},
        freeform_tags={"project": "maia", "env": "cloud", "cost": "always_free"}
    )


def victory_message(inst, public_ip, region, is_a1: bool) -> str:
    sc = inst.shape_config
    specs = f"{sc.ocpus} OCPU / {sc.memory_in_gbs} GB RAM" if sc else inst.shape
    title = "SERVIDOR A1 ALWAYS FREE CAPTURADO" if is_a1 else "MICRO E2.1 ALWAYS FREE CAPTURADA (respaldo)"
    follow = ("🛡️ *Acción:* M.A.I.A. Cloud puede migrarse a esta máquina permanente. Cazador finalizado."
              if is_a1 else "🔁 *El cazador sigue buscando A1* (la Micro solo sirve como relé/túnel: 1 GB RAM).")
    return (
        f"🚨 *M.A.I.A.: {title}* 🚨\n\n"
        f"• *Servidor:* `{inst.display_name}` ({inst.shape})\n"
        f"• *Especificaciones:* `{specs}` ($0.00 Always Free)\n"
        f"• *Región:* `{region}`\n"
        f"• *IP Pública:* `{public_ip}`\n"
        f"• *Conexión:* `ssh -i ~/.ssh/id_maia_cloud ubuntu@{public_ip}`\n"
        f"• *Estado:* `{inst.lifecycle_state}`\n\n{follow}"
    )


def main():
    parser = argparse.ArgumentParser(description="Cazador Always Free OCI para M.A.I.A.")
    parser.add_argument("--dry-run", action="store_true", help="Valida todo sin lanzar instancias")
    parser.add_argument("--once", action="store_true", help="Ejecuta una sola ronda y termina")
    args = parser.parse_args()

    if not args.dry_run and not acquire_lock():
        sys.exit(0)

    logger.info("======================================================================")
    logger.info("  ORACLE CLOUD ALWAYS FREE HUNTER (A1 2/12 -> A1 1/6 -> E2.1.Micro)" + (" [DRY-RUN]" if args.dry_run else ""))
    logger.info("======================================================================")

    try:
        import oci
    except ImportError:
        logger.error("OCI SDK no instalado en el entorno actual.")
        sys.exit(1)

    config_path = os.path.expanduser("~/.oci/config")
    if not os.path.exists(config_path):
        logger.error(f"OCI config no encontrado en: {config_path}")
        sys.exit(1)

    config = oci.config.from_file(config_path)
    tenancy_id = config["tenancy"]
    region = config["region"]
    compute_client, identity_client, net_client, block_client, limits_client = create_resilient_clients(config)
    ad_name = identity_client.list_availability_domains(tenancy_id).data[0].name

    target_subnet = next((s for s in net_client.list_subnets(compartment_id=tenancy_id).data if s.lifecycle_state == "AVAILABLE"), None)
    if not target_subnet:
        logger.error("Error: No se encontró Subnet disponible en la VCN.")
        sys.exit(1)

    images = {
        "VM.Standard.A1.Flex": get_ubuntu_image(compute_client, tenancy_id, "VM.Standard.A1.Flex", "aarch64"),
        "VM.Standard.E2.1.Micro": get_ubuntu_image(compute_client, tenancy_id, "VM.Standard.E2.1.Micro", "x86_64"),
    }
    for shape, img in images.items():
        logger.info(f"Imagen Ubuntu 24.04 para {shape}: {'…' + img[-8:] if img else 'NO ENCONTRADA'}")

    # Solo cazar formas que el AD realmente ofrece (sa-bogota-1 no ofrece E2.1.Micro aunque exista cuota)
    offered = {s.shape for s in oci.pagination.list_call_get_all_results(compute_client.list_shapes, tenancy_id, availability_domain=ad_name).data}
    for shape in list(images):
        if shape not in offered:
            logger.warning(f"La forma {shape} no se ofrece en {ad_name}: se excluye de la caza.")
            images[shape] = None

    ssh_key_path = os.path.expanduser("~/.ssh/id_maia_cloud.pub")
    if not os.path.exists(ssh_key_path):
        ssh_key_path = os.path.expanduser("~/.ssh/id_rsa.pub")
    with open(ssh_key_path, "r", encoding="utf-8") as f:
        ssh_pub_key = f.read().strip()

    logger.info(f"Hunter activo. Región: {region} | AD: {ad_name} | Objetivos: " + " -> ".join(t["key"] for t in TARGETS if images.get(t["shape"])))

    attempt = 1
    backoff = 0.0
    micro_notified = False
    while True:
        throttled = False
        try:
            # 1. ¿Ya tenemos A1? Misión cumplida
            a1 = find_active_instance(compute_client, tenancy_id, A1_NAME)
            if a1:
                public_ip = instance_public_ip(compute_client, net_client, tenancy_id, a1)
                logger.info(f"🎉 ¡INSTANCIA A1 CAPTURADA! Estado: {a1.lifecycle_state} | IP: {public_ip}")
                if a1.lifecycle_state == "RUNNING" and public_ip != "Pendiente":
                    notify_telegram(victory_message(a1, public_ip, region, is_a1=True))
                    logger.info("Aprovisionamiento A1 completado al 100%. Misión cumplida.")
                    break
                time.sleep(20)
                continue

            micro = find_active_instance(compute_client, tenancy_id, MICRO_NAME)
            if micro and not micro_notified and micro.lifecycle_state == "RUNNING":
                public_ip = instance_public_ip(compute_client, net_client, tenancy_id, micro)
                if public_ip != "Pendiente":
                    notify_telegram(victory_message(micro, public_ip, region, is_a1=False))
                    micro_notified = True

            used_gb = used_block_storage_gb(block_client, tenancy_id, ad_name)

            # 2. Recorrer objetivos en orden de preferencia
            for target in TARGETS:
                if target["key"] == "micro" and micro:
                    continue
                image_id = images.get(target["shape"])
                if not image_id:
                    continue
                if used_gb + target["boot_gb"] > ALWAYS_FREE_BLOCK_STORAGE_GB:
                    logger.info(f"Almacenamiento: {used_gb} GB usados + {target['boot_gb']} GB superaría {ALWAYS_FREE_BLOCK_STORAGE_GB} GB gratuitos. Se omite {target['key']}.")
                    continue
                if not free_limit_available(limits_client, tenancy_id, ad_name, target["limits"]):
                    continue

                desc = f"{target['shape']}" + (f" {target['ocpus']:.0f} OCPU / {target['ram']:.0f} GB" if target["ocpus"] else "")
                details = build_launch_details(target, tenancy_id, ad_name, target_subnet.id, image_id, ssh_pub_key)
                if args.dry_run:
                    logger.info(f"[DRY-RUN] Intento #{attempt}: se pediría {desc} (boot {target['boot_gb']} GB, usado {used_gb} GB). Validación OK.")
                    continue

                opc_request_id = str(uuid.uuid4())
                logger.info(f"Intento #{attempt}: Sondeando {desc} Always Free en {region} (token: {opc_request_id[:8]})...")
                try:
                    res = compute_client.launch_instance(details, opc_retry_token=opc_request_id).data
                    logger.info(f"¡PETICIÓN ACEPTADA POR OCI! {desc} OCID: …{res.id[-8:]}")
                    break
                except oci.exceptions.ServiceError as e:
                    msg = str(e.message).lower()
                    if e.status == 429 or "too many requests" in msg:
                        logger.warning("Respuesta 429 OCI (demasiadas peticiones). Pausa con backoff.")
                        throttled = True
                        break
                    if "capacity" in msg:
                        logger.info(f"Capacidad {desc} ocupada en Bogotá.")
                    elif "limitexceeded" in str(e.code).lower():
                        logger.warning(f"Límite excedido para {desc}: {e.message[:140]}")
                    else:
                        logger.warning(f"Respuesta OCI ({desc}): {e.code} - {str(e.message)[:140]}")
                time.sleep(random.uniform(4.0, 9.0))
        except oci.exceptions.ServiceError as e:
            if e.status == 429:
                throttled = True
                logger.warning("Respuesta 429 OCI en consultas. Pausa con backoff.")
            else:
                logger.warning(f"Respuesta OCI: {e.code} - {str(e.message)[:140]}")
        except Exception as ex:
            logger.error(f"Error en ciclo hunter: {ex}")

        if args.dry_run or args.once:
            logger.info("Ronda única completada.")
            break

        attempt += 1
        backoff = min(MAX_BACKOFF, (backoff * 2) or 120.0) if throttled else 0.0
        time.sleep(random.uniform(*BASE_SLEEP) + backoff)


if __name__ == "__main__":
    main()
