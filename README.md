# Apple Find My Sync

A Python microservice that syncs location data from Apple's Find My network to a Traccar server.

It acts as a bridge, fetching location reports from your Apple devices (AirTags, iPhones, etc.) and pushing them to your self-hosted Traccar instance for tracking and visualization.

## Features

- **Automated Sync**: Regularly fetches location history for your devices.
- **Resilient**: Handles network timeouts and rate limits gracefully with a queue-based architecture.
- **Data Integrity**: Ensures no location reports are skipped, even if a fetch fails midway.
- **Traccar Integration**: Pushes location data directly to Traccar using the OsmAnd protocol.

## Setup

### Prerequisites

- Python 3.13+
- `uv` package manager installed.
- A valid Apple ID with 2FA enabled.
- access to a Traccar server.

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

1.  **Prepare `airtag.json`**:
    You need a file named `airtag.json` in the working directory that lists the devices you want to track.
    Follow the [FindMy.py documentation](https://docs.mikealmel.ooo/FindMy.py/getstarted/02-fetching.html) to obtain your device information and create this file. It should contain a list of `FindMyAccessoryMapping` json objects.

2.  **Run the Microservice**:
    The service needs the URL of your Traccar server to push data to.

    ```bash
    # Set the URL via environment variable
    export PUSH_URL="http://your-traccar-server:5055"
    
    # Run the service
    uv run microservice
    ```
    
    *Note: On first run, it will prompt you for your Apple ID credentials and 2FA code to generate a session file (`account.json`).*

## Credits & Acknowledgements

- **[FindMy.py](https://github.com/malmeloo/FindMy.py)**: This project is heavily based on the excellent work by `malmeloo` and their reverse engineering of the Find My protocol. The core interaction with Apple's servers is handled by this library.
- **[Traccar](https://github.com/traccar/traccar)**: The robust open-source GPS tracking platform. Thanks to the Traccar team for their great documentation on custom device integration.

## Transparency

This project was built with the help of LLMs. However, all code has been manually reviewed and tested to ensure it works correctly and safely. It is not just raw AI output. The logic has been verified to ensure it functions as intended.

## License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.
