/* Test-only datagram blackhole. Never bundled in the desktop extension.
 * Interpose only in the fixture process; leave OS networking and DNS alone. */
#define _GNU_SOURCE
#include <sys/socket.h>
#include <netinet/in.h>
#include <stdint.h>
#include <stdio.h>
#include <dlfcn.h>

static unsigned long dropped;
static int discard(int fd, const struct sockaddr *address) {
    int type = 0; socklen_t length = sizeof(type);
    if (getsockopt(fd, SOL_SOCKET, SO_TYPE, &type, &length) || type != SOCK_DGRAM) return 0;
    struct sockaddr_storage peer;
    if (!address) { length = sizeof(peer); if (!getpeername(fd, (struct sockaddr *)&peer, &length)) address = (struct sockaddr *)&peer; }
    if (address && address->sa_family == AF_INET && ntohs(((const struct sockaddr_in *)address)->sin_port) == 53) return 0;
    if (address && address->sa_family == AF_INET6 && ntohs(((const struct sockaddr_in6 *)address)->sin6_port) == 53) return 0;
    dropped++; return 1;
}
static size_t message_size(const struct msghdr *message) {
    size_t size = 0;
    for (size_t i = 0; i < (size_t)message->msg_iovlen; i++) size += message->msg_iov[i].iov_len;
    return size;
}
#ifdef __APPLE__
static ssize_t fixture_sendmsg(int fd, const struct msghdr *message, int flags) {
    if (discard(fd, message->msg_name)) return (ssize_t)message_size(message);
    return sendmsg(fd, message, flags);
}
static ssize_t fixture_sendto(int fd, const void *bytes, size_t size, int flags, const struct sockaddr *address, socklen_t length) {
    if (discard(fd, address)) return (ssize_t)size;
    return sendto(fd, bytes, size, flags, address, length);
}
#define INTERPOSE(replacement, original) \
    __attribute__((used)) static struct { const void *new_fn; const void *old_fn; } interpose_##original \
    __attribute__((section("__DATA,__interpose"))) = {(const void *)(uintptr_t)&replacement, (const void *)(uintptr_t)&original};
INTERPOSE(fixture_sendmsg, sendmsg)
INTERPOSE(fixture_sendto, sendto)
#else
ssize_t sendmsg(int fd, const struct msghdr *message, int flags) {
    if (discard(fd, message->msg_name)) return (ssize_t)message_size(message);
    ssize_t (*original)(int, const struct msghdr *, int) = dlsym(RTLD_NEXT, "sendmsg");
    return original(fd, message, flags);
}
ssize_t sendto(int fd, const void *bytes, size_t size, int flags, const struct sockaddr *address, socklen_t length) {
    if (discard(fd, address)) return (ssize_t)size;
    ssize_t (*original)(int, const void *, size_t, int, const struct sockaddr *, socklen_t) = dlsym(RTLD_NEXT, "sendto");
    return original(fd, bytes, size, flags, address, length);
}
int sendmmsg(int fd, struct mmsghdr *messages, unsigned int count, int flags) {
    for (unsigned int i = 0; i < count; i++) {
        ssize_t size = sendmsg(fd, &messages[i].msg_hdr, flags);
        if (size < 0) return i ? (int)i : -1;
        messages[i].msg_len = (unsigned int)size;
    }
    return (int)count;
}
#endif
__attribute__((constructor)) static void started(void) { fputs("Airshower isolated UDP-drop fixture enabled\n", stderr); }
__attribute__((destructor)) static void finished(void) { fprintf(stderr, "Airshower fixture discarded %lu non-DNS UDP sends\n", dropped); }
