from app import create_app

# The application factory returns the configured app instance
app = create_app()

# This __main__ block is only for local development and will not be used in production
if __name__ == '__main__':
    app.run(debug=True)