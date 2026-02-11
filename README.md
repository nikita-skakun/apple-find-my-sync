# Apple Find My Sync

**apple-find-my-sync** is a headless microservice that retrieves location reports from the Apple Find My network (e.g., AirTags) and pushes them to a Traccar server.

Designed for always-on Linux deployments (VPS, Docker), it requires a **one-time** macOS bootstrap to extract credentials. Once set up, the service runs fully independently, serving as a reliable bridge between Apple's tracking ecosystem and your self-hosted Traccar pipeline.

## macOS Bootstrap Requirement

AirTag support requires access to Apple Find My credentials and cryptographic keys, which can only be obtained from a macOS environment.

**Key points:**
- macOS is required **only once**, during initial setup to generate `airtag.json`.
- Valid options include:
  - A physical Mac
  - A Hackintosh running in a VM
  - A Hackintosh environment running via Docker
- After bootstrapping, synchronization runs headlessly on Linux with no further macOS dependency.

## Use Cases

- **Centralized asset tracking** using Traccar.
- **Server-side aggregation** of Apple Find My locations.
- **Headless deployments** (Linux, Docker, VPS).
- **Mixed tracking backends** (Apple Find My, Google Find My Device, etc.) aggregated in one UI.

## Related Projects

The following projects operate in a similar problem space.

| Project | Traccar Integration | AirTag Support | DIY / OpenHaystack | macOS Required |
| :--- | :--- | :--- | :--- | :--- |
| **apple-find-my-sync** | ✅ Native | ✅ First-class | ❌ (planned) | Yes (bootstrap only) |
| **findmy-traccar-bridge** | ✅ Native | ⚠️ Manual / secondary | ✅ Yes | Yes (for AirTags, implicitly) |
| **FindMy.py** (library) | ⚠️ Building block | ✅ Yes | ✅ Yes | Yes (for AirTags) |

- **apple-find-my-sync**: Optimized for official Apple devices (AirTags), clean separation between bootstrap and runtime, designed for long-running headless deployments.
- **findmy-traccar-bridge**: Python-based bridge using FindMy.py, well-suited for DIY/OpenHaystack trackers.
- **FindMy.py**: Core library used by multiple projects. Not a Traccar integration by itself.

## Setup

### Prerequisites

- Python 3.13+
- `uv` package manager installed.
- Access to a Traccar server.
- **Initial Setup**: A macOS environment (physical or virtual) to extract keys.

### Installation

1.  Clone this repository:
    ```bash
    git clone https://github.com/yourusername/apple-find-my-sync.git
    cd apple-find-my-sync
    ```

2.  Sync dependencies:
    ```bash
    uv sync
    ```

### Configuration

1.  **Bootstrap on macOS**:
    Follow the [FindMy.py documentation](https://docs.mikealmel.ooo/FindMy.py/getstarted/02-fetching.html) on your macOS machine to obtain your device information and generate the `airtag.json` file. This file contains the keys required to decode location reports.

2.  **Deploy to Linux**:
    Copy the generated `airtag.json` file to your Linux server in the project directory.

3.  **Run the Microservice**:
    The service needs the URL of your Traccar server to push data to.

    ```bash
    # Set the URL via environment variable
    export PUSH_URL="http://your-traccar-server:5055"
    
    # Run the service
    uv run microservice
    ```
    
    *Note: On first run, it will prompt you for your Apple ID credentials and 2FA code to generate a session file (`account.json`). This session file is reused across restarts.*

## Future Work

- **OpenHaystack integration**: Support ingesting locations from OpenHaystack-compatible DIY trackers, allowing mixed environments of Official Apple devices (AirTags) and Custom Find My beacons to be synchronized into Traccar through a single pipeline.

## Credits & Acknowledgements

- **[FindMy.py](https://github.com/malmeloo/FindMy.py)**: This project is heavily based on the excellent work by `malmeloo` and their reverse engineering of the Find My protocol. The core interaction with Apple's servers is handled by this library.
- **[Traccar](https://github.com/traccar/traccar)**: The robust open-source GPS tracking platform. Thanks to the Traccar team for their great documentation on custom device integration.

## Transparency

This project was built with the help of LLMs. However, all code has been manually reviewed and tested to ensure it works correctly and safely. It is not just raw AI output. The logic has been verified to ensure it functions as intended.

## License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.
