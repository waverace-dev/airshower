/* Keep the profile leased with an OS lock. EOF releases it after a host crash;
 * the lock file itself is never unlinked or reclaimed by PID guessing. */
#include <sys/file.h>
#include <sys/stat.h>
#include <fcntl.h>
#include <unistd.h>
#include <stdio.h>
#include <errno.h>

int main(int argc, char **argv) {
    if (argc != 2) return 3;
    int fd = open(argv[1], O_CREAT | O_RDWR | O_NOFOLLOW | O_NONBLOCK, 0600);
    struct stat info;
    if (fd < 0 || fstat(fd, &info) || !S_ISREG(info.st_mode) ||
        info.st_uid != getuid() || (info.st_mode & 077) || info.st_nlink != 1) return 3;
    if (flock(fd, LOCK_EX | LOCK_NB)) return 2;
    if (write(STDOUT_FILENO, "ready\n", 6) != 6) return 3;
    char buffer[64];
    while (1) {
        ssize_t n = read(STDIN_FILENO, buffer, sizeof(buffer));
        if (n == 0) break;
        if (n < 0 && errno != EINTR) break;
    }
    close(fd);
    return 0;
}
