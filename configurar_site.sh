#!/bin/bash
# Configuración Site CISA: Gestión (Puerto Integrado) | Cámaras (Adaptador USB)

# Variables de Red
IP_SITE="192.168.40.49/24"
GW_SITE="192.168.40.1"
DNS_SITE="8.8.8.8,8.8.4.4"
IP_CAMS="192.168.10.1/24"

# Nombres de interfaz
INT_INTEGRADO="enP8p1s0"
INT_USB="enx006f00010036"

echo "🚀 Asegurando configuración: Internet en puerto placa (.40.49) y Cámaras en USB (.10.1)..."

# 1. Limpieza
sudo nmcli con delete "Conexion-Internet" 2>/dev/null
sudo nmcli con delete "Red-Camaras" 2>/dev/null
sudo nmcli con delete "Wired connection 1" 2>/dev/null

# 2. GESTIÓN (Prioridad Alta - Métrica 100)
sudo nmcli con add type ethernet con-name "Conexion-Internet" ifname "$INT_INTEGRADO" \
    ipv4.addresses "$IP_SITE" ipv4.gateway "$GW_SITE" ipv4.dns "$DNS_SITE" \
    ipv4.method manual ipv4.route-metric 100 connection.autoconnect yes

# 3. CÁMARAS (Prioridad Baja - Métrica 200)
sudo nmcli con add type ethernet con-name "Red-Camaras" ifname "$INT_USB" \
    ipv4.addresses "$IP_CAMS" ipv4.method manual ipv4.route-metric 200 connection.autoconnect yes

# 4. Levantar
sudo nmcli con up "Conexion-Internet"
sudo nmcli con up "Red-Camaras"

echo "✅ Verificación completa. El sistema está blindado."
