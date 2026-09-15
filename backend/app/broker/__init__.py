"""Optional broker-account integration (Broker page). Entirely opt-in -
nothing here runs unless the user connects an account: market data stays on
Yahoo Finance (app.data.fetcher) and the strategy's own signals never place
an order on their own. This package only ever sends an order when the user
submits one themselves via the Broker page's manual order form."""
