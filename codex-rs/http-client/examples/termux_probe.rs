//! Device-side DNS and TLS checks. No account credentials are used.

use std::io::Write;
use std::net::TcpStream;
use std::net::ToSocketAddrs;
use std::time::Duration;

use codex_http_client::build_reqwest_client_with_custom_ca;
use codex_http_client::build_rustls_client_config_with_custom_ca;

#[tokio::main(flavor = "current_thread")]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    let host = "chatgpt.com";
    let timeout = Duration::from_secs(15);
    let addresses: Vec<_> = (host, 443).to_socket_addrs()?.collect();
    if addresses.is_empty() {
        return Err("system DNS returned no addresses".into());
    }
    println!("System DNS: {} addresses", addresses.len());

    // A direct socket and a client without explicit proxy discovery exercise
    // the device's normal networking/TUN route, without an HTTP listener.
    let client = build_reqwest_client_with_custom_ca(
        reqwest::Client::builder().no_proxy().timeout(timeout),
    )?;
    let response = client.head("https://chatgpt.com/").send().await?;
    println!("HTTPS certificate verified; HTTP status: {}", response.status());

    let mut connected = None;
    for address in addresses {
        if let Ok(stream) = TcpStream::connect_timeout(&address, timeout) {
            connected = Some(stream);
            break;
        }
    }
    let socket = connected.ok_or("could not connect to any system DNS address")?;
    socket.set_read_timeout(Some(timeout))?;
    socket.set_write_timeout(Some(timeout))?;
    let config = build_rustls_client_config_with_custom_ca()?;
    let connection = rustls::ClientConnection::new(config, host.try_into()?)?;
    let mut tls = rustls::StreamOwned::new(connection, socket);
    tls.write_all(b"HEAD / HTTP/1.1\r\nHost: chatgpt.com\r\nConnection: close\r\n\r\n")?;
    tls.flush()?;
    println!("WebSocket rustls trust configuration: TLS handshake verified");
    println!("This does not test account login, a WebSocket upgrade or model responses.");
    Ok(())
}
