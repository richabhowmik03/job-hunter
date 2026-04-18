import { createTheme } from '@mui/material/styles'

/** Minimal SaaS-style light theme: soft gray canvas, indigo primary, rounded inputs. */
export const appTheme = createTheme({
  palette: {
    mode: 'light',
    primary: {
      main: '#2f4fd8',
      dark: '#1e3bb0',
      light: '#5a6fe8',
    },
    background: {
      default: '#f0f2f7',
      paper: '#ffffff',
    },
    text: {
      primary: '#1a1d26',
      secondary: '#5c6378',
    },
    divider: 'rgba(26, 29, 38, 0.08)',
  },
  shape: {
    borderRadius: 10,
  },
  typography: {
    fontFamily:
      '"DM Sans", "Inter", system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif',
    h4: {
      fontWeight: 700,
      letterSpacing: '-0.02em',
      fontSize: '1.75rem',
    },
    subtitle1: {
      fontSize: '0.95rem',
      lineHeight: 1.5,
      color: '#5c6378',
    },
    subtitle2: {
      fontWeight: 600,
      fontSize: '0.8125rem',
      letterSpacing: '0.06em',
      textTransform: 'uppercase',
      color: '#7a8194',
    },
  },
  components: {
    MuiCssBaseline: {
      styleOverrides: {
        body: {
          backgroundColor: '#f0f2f7',
        },
      },
    },
    MuiTextField: {
      defaultProps: {
        variant: 'outlined',
        size: 'medium',
      },
      styleOverrides: {
        root: {
          '& .MuiOutlinedInput-root': {
            backgroundColor: '#fff',
            borderRadius: 10,
          },
        },
      },
    },
    MuiButton: {
      styleOverrides: {
        root: {
          borderRadius: 10,
          textTransform: 'none',
          fontWeight: 600,
          fontSize: '0.95rem',
          paddingTop: 12,
          paddingBottom: 12,
          boxShadow: 'none',
          '&:hover': {
            boxShadow: '0 4px 14px rgba(47, 79, 216, 0.28)',
          },
          '&.MuiButton-containedPrimary:hover': {
            boxShadow: '0 4px 14px rgba(47, 79, 216, 0.32)',
          },
        },
      },
    },
    MuiPaper: {
      styleOverrides: {
        root: {
          backgroundImage: 'none',
        },
        outlined: {
          borderColor: 'rgba(26, 29, 38, 0.1)',
        },
      },
    },
    MuiInputLabel: {
      styleOverrides: {
        root: {
          fontSize: '0.7rem',
          fontWeight: 600,
          letterSpacing: '0.08em',
          textTransform: 'uppercase',
          color: '#7a8194',
        },
      },
    },
    MuiAlert: {
      styleOverrides: {
        root: {
          borderRadius: 10,
        },
      },
    },
  },
})
