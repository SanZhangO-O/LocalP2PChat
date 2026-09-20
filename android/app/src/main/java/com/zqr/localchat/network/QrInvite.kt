package com.zqr.localchat.network

import java.net.URLDecoder
import java.net.URLEncoder

/**
 * QR invite payloads: contact cards and group invites as compact URLs.
 *
 * The payload is plain text (a URL) so both a QR image and copy/paste carry
 * the same bytes, and both platforms encode and decode it identically
 * (Windows parity: localchat/qrshare.py):
 *
 *     localchat://contact?v=1&n=<name>&f=<security code>&ip=<addr>&p=<port>
 *     localchat://group?v=1&g=<8-digit id>&n=<group name>&ip=<addr>&p=<port>&r=<relay>
 *
 * Rules:
 * - `v` is the payload format version; unknown versions are rejected.
 * - Values are UTF-8 percent-encoded; decoding accepts both %XX and the
 *   "+"-encoded spaces URLEncoder produces (URLDecoder semantics).
 * - contact: `ip` required; `f` (the peer's 安全码 fingerprint) and `n` are
 *   optional; `p` defaults to [Constants.TCP_PORT].
 * - group: `g` required (digits only, 8 digits); `ip`/`p` (a member's LAN
 *   endpoint — any member can be the join entry point) and `r` (relay
 *   server host:port) are optional.
 * - The group PASSWORD is NEVER part of the payload: scanning still requires
 *   the joiner to type the group password. Any password-looking parameter is
 *   ignored on decode.
 */
data class ContactInvite(
    val name: String = "",
    val fingerprint: String = "",
    val ip: String = "",
    val port: Int = Constants.TCP_PORT
)

data class GroupInvite(
    val groupId: String = "",
    val name: String = "",
    val ip: String = "",
    val port: Int = Constants.TCP_PORT,
    val relay: String = ""
)

class QrPayloadException(message: String) : Exception(message)

object QrInvite {

    const val SCHEME = "localchat"
    const val KIND_CONTACT = "contact"
    const val KIND_GROUP = "group"
    const val PAYLOAD_VERSION = "1"

    private fun encodeValue(value: String): String =
        URLEncoder.encode(value, "UTF-8")

    private fun decodeValue(value: String): String =
        URLDecoder.decode(value, "UTF-8")

    fun encodeContactInvite(
        name: String,
        fingerprint: String,
        ip: String,
        port: Int = Constants.TCP_PORT
    ): String {
        val parts = mutableListOf("v=$PAYLOAD_VERSION")
        if (name.isNotEmpty()) parts += "n=" + encodeValue(name)
        if (fingerprint.isNotEmpty()) parts += "f=" + encodeValue(fingerprint)
        if (ip.isNotEmpty()) parts += "ip=" + encodeValue(ip)
        if (port in 1..65535 && port != Constants.TCP_PORT) parts += "p=$port"
        return "$SCHEME://$KIND_CONTACT?" + parts.joinToString("&")
    }

    fun encodeGroupInvite(
        groupId: String,
        name: String = "",
        ip: String = "",
        port: Int = Constants.TCP_PORT,
        relay: String = ""
    ): String {
        val digits = groupId.filter { it.isDigit() }
        val parts = mutableListOf("v=$PAYLOAD_VERSION", "g=$digits")
        if (name.isNotEmpty()) parts += "n=" + encodeValue(name)
        if (ip.isNotEmpty()) parts += "ip=" + encodeValue(ip)
        if (port in 1..65535 && port != Constants.TCP_PORT) parts += "p=$port"
        if (relay.isNotEmpty()) parts += "r=" + encodeValue(relay)
        return "$SCHEME://$KIND_GROUP?" + parts.joinToString("&")
    }

    /** Parses a QR payload; throws [QrPayloadException] on anything that is
     *  not a current-format invite. Unknown parameters are ignored (forward
     *  compatibility); a password-looking parameter is ignored too. */
    fun parse(text: String): Any {
        val raw = text.trim()
        val prefix = "$SCHEME://"
        if (!raw.startsWith(prefix)) {
            throw QrPayloadException("not a localchat payload")
        }
        val rest = raw.substring(prefix.length)
        val qIndex = rest.indexOf('?')
        val kindRaw = if (qIndex >= 0) rest.substring(0, qIndex) else rest
        val query = if (qIndex >= 0) rest.substring(qIndex + 1) else ""
        val kind = kindRaw.trim().trimEnd('/')
        val fields = parseQuery(query)
        if ((fields["v"] ?: PAYLOAD_VERSION) != PAYLOAD_VERSION) {
            throw QrPayloadException("unsupported payload version")
        }
        return when (kind) {
            KIND_CONTACT -> parseContact(fields)
            KIND_GROUP -> parseGroup(fields)
            else -> throw QrPayloadException("unknown payload kind")
        }
    }

    private fun parseQuery(query: String): Map<String, String> {
        if (query.isEmpty()) return emptyMap()
        val fields = LinkedHashMap<String, String>()
        for (chunk in query.split('&')) {
            if (chunk.isEmpty()) continue
            val idx = chunk.indexOf('=')
            val key: String
            val value: String
            if (idx >= 0) {
                key = chunk.substring(0, idx)
                value = chunk.substring(idx + 1)
            } else {
                key = chunk
                value = ""
            }
            fields[decodeValue(key)] = decodeValue(value)
        }
        return fields
    }

    private fun isValidHost(host: String): Boolean {
        if (host.isEmpty() || host.length > 253) return false
        val parts = host.split(".")
        val dottedQuad = parts.size == 4 && parts.all { p ->
            p.isNotEmpty() && p.all { it in '0'..'9' } && p.toIntOrNull() in 0..255
        }
        if (dottedQuad) return true
        // tolerate IPv6 literals in scanned payloads (joining still needs a
        // LAN address; validation only rejects obvious garbage)
        return host.contains(':') && host.all { it.isDigit() || it in "abcdefABCDEF:." }
    }

    private fun validPort(port: Int): Boolean = port in 1..65535

    private fun isValidRelay(relay: String): Boolean {
        val idx = relay.lastIndexOf(':')
        if (idx <= 0 || idx == relay.length - 1) return false
        val portText = relay.substring(idx + 1)
        val port = portText.toIntOrNull() ?: return false
        return port in 1..65535
    }

    private fun parsePort(fields: Map<String, String>): Int? {
        val portText = fields["p"] ?: ""
        if (portText.isEmpty()) return Constants.TCP_PORT
        if (!portText.all { it.isDigit() }) return null
        return portText.toIntOrNull()
    }

    private fun parseContact(fields: Map<String, String>): ContactInvite {
        val ip = fields["ip"] ?: ""
        if (ip.isEmpty() || !isValidHost(ip)) {
            throw QrPayloadException("contact payload without a valid address")
        }
        val port = parsePort(fields)
        if (port == null || !validPort(port)) throw QrPayloadException("invalid port")
        val fingerprint = (fields["f"] ?: "").uppercase()
        if (fingerprint.isNotEmpty() &&
            (fingerprint.length != 16 || fingerprint.any { it !in "0123456789ABCDEF" })
        ) {
            throw QrPayloadException("invalid security code")
        }
        return ContactInvite(
            name = fields["n"] ?: "",
            fingerprint = fingerprint,
            ip = ip,
            port = port
        )
    }

    private fun parseGroup(fields: Map<String, String>): GroupInvite {
        val digits = (fields["g"] ?: "").filter { it.isDigit() }
        if (digits.length != 8) {
            throw QrPayloadException("group payload without a valid numeric id")
        }
        val ip = fields["ip"] ?: ""
        val portText = fields["p"] ?: ""
        val port = parsePort(fields)
        if (ip.isNotEmpty()) {
            if (!isValidHost(ip)) throw QrPayloadException("invalid address")
            if (port == null || !validPort(port)) throw QrPayloadException("invalid port")
        } else if (portText.isNotEmpty() && (port == null || !validPort(port))) {
            throw QrPayloadException("invalid port")
        }
        val relay = fields["r"] ?: ""
        if (relay.isNotEmpty() && !isValidRelay(relay)) {
            throw QrPayloadException("invalid relay address")
        }
        return GroupInvite(
            groupId = digits,
            name = fields["n"] ?: "",
            ip = ip,
            // the branches above verified the port whenever it was declared;
            // an undeclared port is the standard one
            port = port ?: Constants.TCP_PORT,
            relay = relay
        )
    }
}
