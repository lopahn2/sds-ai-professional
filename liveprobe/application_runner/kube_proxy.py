"""Connect the Kubernetes SDK to a runner's private kubectl proxy socket."""
import os
from pathlib import Path
import socket
import stat


def use_private_socket(api, path):
    from urllib3.connection import HTTPConnection
    from urllib3.connectionpool import HTTPConnectionPool

    path = Path(path)
    if not path.is_absolute() or path.parent.resolve() != path.parent:
        raise ValueError('private_kubernetes_socket_required')
    directory = path.parent.stat()
    endpoint = path.lstat()
    if (directory.st_uid != os.getuid() or directory.st_mode & 0o077 or
            not stat.S_ISSOCK(endpoint.st_mode) or endpoint.st_uid != os.getuid()):
        raise ValueError('private_kubernetes_socket_required')

    class LocalConnection(HTTPConnection):
        def _new_conn(self):
            connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                if isinstance(self.timeout, (int, float)):
                    connection.settimeout(self.timeout)
                connection.connect(str(path))
                return connection
            except OSError:
                connection.close()
                raise

    class LocalPool(HTTPConnectionPool):
        ConnectionCls = LocalConnection

    # Override this client's HTTP transport only. Keep the SDK's query, raw log
    # streaming, status handling and timeout behavior for every registered job.
    manager = api.rest_client.pool_manager
    manager.pool_classes_by_scheme = {'http': LocalPool}
