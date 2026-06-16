# Authentication Middleware Research

## Current State
- Express app in src/app.js
- Existing routes in src/routes/
- No authentication currently implemented
- JWT tokens will be used for auth

## Requirements
- Add JWT authentication middleware
- Protect /api/protected/* routes
- Token validation with secret key
- Return 401 for invalid/missing tokens

## Dependencies
- jsonwebtoken package
- express-async-errors for async error handling

## Key Files
- src/app.js (main app setup)
- src/middleware/auth.js (new middleware)
- src/routes/protected.js (protected routes)
- src/config/config.js (configuration)
